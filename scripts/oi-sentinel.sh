#!/usr/bin/env bash
# oi-sentinel — the out-of-process dead-man switch for the OI stack.
#
# Runs every minute from a systemd timer (see scripts/systemd/). It owns the
# failure classes the backend process CANNOT fix about itself: a frozen event
# loop, a dead container, a dead docker daemon, a full disk, an expiring TLS
# cert, stale backups, a dead Caddy. It NEVER touches the broker session —
# rotations belong to the in-process SessionSteward, and an out-of-process login
# would recreate the very storm the steward exists to prevent.
#
# Decision table (probe = GET /api/health/strict on localhost):
#   docker daemon dead            -> systemctl start docker + page
#   container not running         -> compose up -d <svc> + page   (24/7 — this IS
#                                    the morning assurance; no separate timer)
#   strict 200                    -> healthy (page once on recovery)
#   strict 404                    -> old build: fall back to basic /api/health
#   strict 503 restart_recommended:false
#                                 -> page only (broker down / circuit parked /
#                                    db-side cause — a restart fixes nothing)
#   strict 503/timeout, in window -> count; K consecutive AND container uptime
#                                    > 10 min (never race in-process recovery,
#                                    incl. the steward's own os._exit restart)
#                                    -> snapshot logs -> compose restart backend
#                                    -> capped MAX_RESTARTS_PER_DAY (holidays
#                                    missing from the calendar can 503 all day)
#   crash-loop (restarts rising, age <60s)
#                                 -> page only (bad migration = human-only fix)
#
# Subcommands:
#   oi-sentinel                 one monitoring cycle (what the timer runs)
#   oi-sentinel test-alert MSG  send a Telegram message, exit non-zero on failure
#   oi-sentinel alert MSG       raw send (used by the oi-alert@ OnFailure unit)
#   oi-sentinel status          print state + one live probe
#   oi-sentinel reset-day       zero the daily counters (post-drill cleanup)
set -uo pipefail

# ----------------------------------------------------------------- config
OI_REPO="${OI_REPO:-/root/nifty-oi}"
STATE_DIR="${STATE_DIR:-/var/lib/oi-sentinel}"
LOG_DIR="${LOG_DIR:-/var/log/oi-sentinel}"
K_FAILS="${K_FAILS:-5}"
MAX_RESTARTS_PER_DAY="${MAX_RESTARTS_PER_DAY:-2}"
GRACE_AFTER_START_S="${GRACE_AFTER_START_S:-600}"
DEPLOY_MARKER_TTL_S="${DEPLOY_MARKER_TTL_S:-900}"
REALERT_S="${REALERT_S:-1800}"
DISK_ALERT_PCT="${DISK_ALERT_PCT:-85}"
CERT_ALERT_DAYS="${CERT_ALERT_DAYS:-14}"
BACKUP_MAX_AGE_H="${BACKUP_MAX_AGE_H:-26}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/nifty-oi}"
EDGE_URL="${EDGE_URL:-https://oialgo.tech/}"
HEALTHCHECKS_URL="${HEALTHCHECKS_URL:-}"          # external dead-man ping (optional)
OI_SENTINEL_FORCE_WINDOW="${OI_SENTINEL_FORCE_WINDOW:-}"  # =1 for drills

# Optional overrides file (root:600). Holds sentinel tunables + HEALTHCHECKS_URL.
[[ -f /etc/oi-sentinel.env ]] && . /etc/oi-sentinel.env

API_BIND=$(grep -E '^API_BIND=' "$OI_REPO/.env" 2>/dev/null | cut -d= -f2- || true)
API="http://${API_BIND:-127.0.0.1:8000}"
COMPOSE=(docker compose --env-file "$OI_REPO/.env" -f "$OI_REPO/docker/docker-compose.yml")
# Telegram creds come from the repo .env (single home for the secret; already
# chmod 600 by harden-vps.sh). grep, never `source` — .env may contain chars
# bash would interpret.
TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$OI_REPO/.env" 2>/dev/null | cut -d= -f2- || true)
TG_CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "$OI_REPO/.env" 2>/dev/null | cut -d= -f2- || true)

STATE_FILE="$STATE_DIR/state"
mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true

# ----------------------------------------------------------------- helpers
now_epoch() { date +%s; }
ist_date()  { TZ=Asia/Kolkata date +%F; }
ist_hm()    { TZ=Asia/Kolkata date +%H%M; }
ist_dow()   { TZ=Asia/Kolkata date +%u; }

say() { echo "[oi-sentinel] $*"; }

send_telegram() {
  local msg="$1"
  [[ -z "$TG_TOKEN" || -z "$TG_CHAT" ]] && { say "telegram unconfigured (msg: $msg)"; return 1; }
  curl -m 10 -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
       -d chat_id="$TG_CHAT" --data-urlencode text="[oi-sentinel] $(TZ=Asia/Kolkata date '+%H:%M IST') $msg" \
       >/dev/null 2>&1
}

alert() {  # never fails the cycle
  send_telegram "$1" || say "telegram send failed (ignored): $1"
}

load_state() {
  STATUS=UP; FAILS=0; RESTARTS_TODAY=0; RESTARTS_DATE=""; LAST_DOWN_ALERT=0
  HEARTBEAT_DATE=""; DISK_ALERT_DATE=""; CERT_ALERT_DATE=""; BACKUP_ALERT_DATE=""
  EDGE_ALERT=0; DOCKER_ALERT=0; DEGRADED_ALERT=0
  [[ -f "$STATE_FILE" ]] && . "$STATE_FILE"
  # Daily rollover (IST).
  if [[ "$RESTARTS_DATE" != "$(ist_date)" ]]; then
    RESTARTS_TODAY=0; RESTARTS_DATE=$(ist_date)
  fi
}

save_state() {
  local tmp="$STATE_FILE.tmp"
  {
    echo "STATUS=$STATUS"
    echo "FAILS=$FAILS"
    echo "RESTARTS_TODAY=$RESTARTS_TODAY"
    echo "RESTARTS_DATE=$RESTARTS_DATE"
    echo "LAST_DOWN_ALERT=$LAST_DOWN_ALERT"
    echo "HEARTBEAT_DATE=$HEARTBEAT_DATE"
    echo "DISK_ALERT_DATE=$DISK_ALERT_DATE"
    echo "CERT_ALERT_DATE=$CERT_ALERT_DATE"
    echo "BACKUP_ALERT_DATE=$BACKUP_ALERT_DATE"
    echo "EDGE_ALERT=$EDGE_ALERT"
    echo "DOCKER_ALERT=$DOCKER_ALERT"
    echo "DEGRADED_ALERT=$DEGRADED_ALERT"
  } > "$tmp" && mv "$tmp" "$STATE_FILE"
}

in_watch_window() {
  [[ "$OI_SENTINEL_FORCE_WINDOW" == "1" ]] && return 0
  local dow hm
  dow=$(ist_dow); hm=$(ist_hm)
  [[ $dow -le 5 && $hm -ge 0855 && $hm -le 1545 ]]
}

throttled_down_alert() {
  local msg="$1" now
  now=$(now_epoch)
  if (( now - LAST_DOWN_ALERT >= REALERT_S )); then
    LAST_DOWN_ALERT=$now
    alert "$msg"
  fi
}

container_id() { "${COMPOSE[@]}" ps -q "$1" 2>/dev/null | head -1; }

container_running() {
  local id; id=$(container_id "$1")
  [[ -n "$id" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" == "true" ]]
}

container_uptime_s() {
  local id started; id=$(container_id backend)
  [[ -z "$id" ]] && { echo 999999; return; }
  started=$(docker inspect -f '{{.State.StartedAt}}' "$id" 2>/dev/null)
  [[ -z "$started" ]] && { echo 999999; return; }
  echo $(( $(now_epoch) - $(date -d "$started" +%s 2>/dev/null || echo 0) ))
}

snapshot_logs() {
  local out="$LOG_DIR/pre-restart-$(now_epoch).log"
  "${COMPOSE[@]}" logs --no-color --tail 2000 backend > "$out" 2>/dev/null || true
  docker inspect "$(container_id backend)" >> "$out" 2>/dev/null || true
  find "$LOG_DIR" -name 'pre-restart-*' -mtime +14 -delete 2>/dev/null || true
  echo "$out"
}

# ----------------------------------------------------------------- daily block
daily_checks() {
  local today; today=$(ist_date)
  [[ "$HEARTBEAT_DATE" == "$today" ]] && return 0
  [[ "$(ist_hm)" < "0850" ]] && return 0

  local disk cert_days backup_age_h="n/a" notes=""
  disk=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
  if (( disk >= DISK_ALERT_PCT )) && [[ "$DISK_ALERT_DATE" != "$today" ]]; then
    DISK_ALERT_DATE=$today
    alert "🚨 Disk at ${disk}% — DB writes stop when it fills. Prune docker images / old backups."
  fi

  cert_days="?"
  if [[ -n "$EDGE_URL" ]]; then
    local host end_epoch
    host=$(echo "$EDGE_URL" | sed -E 's#https?://([^/]+)/?.*#\1#')
    end_epoch=$(echo | timeout 10 openssl s_client -servername "$host" -connect "$host:443" 2>/dev/null \
      | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    if [[ -n "$end_epoch" ]]; then
      cert_days=$(( ( $(date -d "$end_epoch" +%s) - $(now_epoch) ) / 86400 ))
      if (( cert_days < CERT_ALERT_DAYS )) && [[ "$CERT_ALERT_DATE" != "$today" ]]; then
        CERT_ALERT_DATE=$today
        alert "🚨 TLS cert for $host expires in ${cert_days}d — check Caddy's ACME renewal."
      fi
    fi
  fi

  local newest
  newest=$(ls -t "$BACKUP_DIR"/oi_*.sql.gz 2>/dev/null | head -1)
  if [[ -n "$newest" ]]; then
    backup_age_h=$(( ( $(now_epoch) - $(stat -c %Y "$newest") ) / 3600 ))
    if (( backup_age_h > BACKUP_MAX_AGE_H )) && [[ "$BACKUP_ALERT_DATE" != "$today" ]]; then
      BACKUP_ALERT_DATE=$today
      alert "🚨 Newest DB backup is ${backup_age_h}h old (cap ${BACKUP_MAX_AGE_H}h) — check oi-backup.timer."
    fi
  elif [[ "$BACKUP_ALERT_DATE" != "$today" ]]; then
    BACKUP_ALERT_DATE=$today
    alert "🚨 No DB backups found in $BACKUP_DIR — install oi-backup.timer."
  fi

  HEARTBEAT_DATE=$today
  alert "💚 sentinel alive · backend $STATUS · disk ${disk}% · cert ${cert_days}d · backup ${backup_age_h}h · restarts today $RESTARTS_TODAY/$MAX_RESTARTS_PER_DAY$notes"
}

edge_check() {
  [[ -z "$EDGE_URL" ]] && return 0
  if curl -m 10 -fsS -o /dev/null "$EDGE_URL" 2>/dev/null; then
    EDGE_ALERT=0
  else
    local now; now=$(now_epoch)
    if (( now - EDGE_ALERT >= REALERT_S )); then
      EDGE_ALERT=$now
      alert "⚠️ Public site $EDGE_URL unreachable while localhost backend is $STATUS — check Caddy (systemctl status caddy) / DNS."
    fi
  fi
}

# ----------------------------------------------------------------- main cycle
cycle() {
  load_state

  # External dead-man ping: fires every run, so its ABSENCE means this box,
  # systemd, or the sentinel itself is dead — the one failure Telegram can't report.
  [[ -n "$HEALTHCHECKS_URL" ]] && curl -m 10 -fsS "$HEALTHCHECKS_URL" >/dev/null 2>&1 || true

  # Docker daemon itself.
  if ! docker info >/dev/null 2>&1; then
    local now; now=$(now_epoch)
    if (( now - DOCKER_ALERT >= REALERT_S )); then
      DOCKER_ALERT=$now
      alert "🚨 Docker daemon unreachable — attempting systemctl start docker."
    fi
    systemctl start docker >/dev/null 2>&1 || true
    STATUS=DOWN; save_state; return
  fi
  DOCKER_ALERT=0

  # Containers present? (24/7 — heals the unless-stopped-after-down trap too.)
  local svc started_any=0
  for svc in timescaledb backend frontend; do
    if ! container_running "$svc"; then
      say "$svc not running — compose up -d $svc"
      "${COMPOSE[@]}" up -d "$svc" >/dev/null 2>&1 || true
      started_any=1
    fi
  done
  if (( started_any )); then
    alert "⚠️ One or more containers were not running — started them (compose up -d)."
    FAILS=0; STATUS=DOWN; save_state; return
  fi

  # Crash-loop signature: young container restarting repeatedly = human-only fix.
  local uptime restarts
  uptime=$(container_uptime_s)
  restarts=$(docker inspect -f '{{.RestartCount}}' "$(container_id backend)" 2>/dev/null || echo 0)
  if (( uptime < 60 && restarts >= 3 )); then
    throttled_down_alert "🚨 Backend is CRASH-LOOPING (restarts=$restarts, age=${uptime}s) — likely a bad migration/config. NOT auto-restarting; SSH in: scripts/deploy.sh logs --tail 100 backend"
    STATUS=DOWN; save_state; return
  fi

  # The probe.
  local code body
  body=$(curl -m 10 -s "$API/api/health/strict" 2>/dev/null)
  code=$(curl -m 10 -s -o /dev/null -w '%{http_code}' "$API/api/health/strict" 2>/dev/null || echo 000)
  if [[ "$code" == "404" ]]; then
    # Old build without strict — basic health 200 counts as healthy.
    code=$(curl -m 10 -s -o /dev/null -w '%{http_code}' "$API/api/health" 2>/dev/null || echo 000)
    body='{"fallback":"basic"}'
  fi

  if [[ "$code" == "200" ]]; then
    if [[ "$STATUS" != "UP" ]]; then
      alert "✅ Backend healthy again."
    fi
    if echo "$body" | grep -q '"degraded"'; then
      local now; now=$(now_epoch)
      if (( now - DEGRADED_ALERT >= REALERT_S )); then
        DEGRADED_ALERT=$now
        alert "🟡 DEGRADED: dashboard is running on REST failover (WS feed down; steward escalating). Data continues at reduced cadence."
      fi
    else
      DEGRADED_ALERT=0
    fi
    STATUS=UP; FAILS=0
    edge_check; daily_checks; save_state; return
  fi

  # Unhealthy. Cause-aware: never restart for what a restart can't fix.
  if [[ "$code" == "503" ]] && echo "$body" | grep -q '"restart_recommended": *false'; then
    throttled_down_alert "⚠️ Backend degraded ($(echo "$body" | head -c 200)) — external cause; NOT restarting."
    STATUS=DOWN; FAILS=0
    edge_check; daily_checks; save_state; return
  fi

  if ! in_watch_window; then
    # Off-hours: transition alert only; no counting, no restarts.
    if [[ "$STATUS" == "UP" ]]; then
      throttled_down_alert "⚠️ Backend probe failing off-hours (code=$code). Will act during market hours."
    fi
    STATUS=DOWN; FAILS=0
    edge_check; daily_checks; save_state; return
  fi

  # In window: grace for deploys and young containers (in-process recovery,
  # including the steward's own os._exit restart, always gets the first shot).
  local marker="$STATE_DIR/deploy-in-progress"
  if [[ -f "$marker" ]] && (( $(now_epoch) - $(cat "$marker" 2>/dev/null || echo 0) < DEPLOY_MARKER_TTL_S )); then
    say "deploy in progress — not counting"
    save_state; return
  fi
  if (( uptime < GRACE_AFTER_START_S )); then
    say "backend only ${uptime}s old — yielding to in-process recovery"
    save_state; return
  fi

  FAILS=$((FAILS + 1))
  say "unhealthy (code=$code) — consecutive fails: $FAILS/$K_FAILS"
  if [[ "$STATUS" == "UP" ]]; then
    throttled_down_alert "⚠️ Backend unhealthy during market hours (code=$code, $FAILS/$K_FAILS before restart)."
  fi
  STATUS=DOWN

  if (( FAILS >= K_FAILS )); then
    if (( RESTARTS_TODAY >= MAX_RESTARTS_PER_DAY )); then
      throttled_down_alert "🚨 Restart cap reached ($RESTARTS_TODAY/$MAX_RESTARTS_PER_DAY today) — NOT restarting again. If today is an NSE holiday missing from data/nse_holidays.json this is expected noise; otherwise SSH in NOW."
    else
      local snap; snap=$(snapshot_logs)
      say "restarting backend (snapshot: $snap)"
      "${COMPOSE[@]}" restart backend >/dev/null 2>&1 || true
      RESTARTS_TODAY=$((RESTARTS_TODAY + 1)); FAILS=0
      alert "🔄 Restarted the backend ($RESTARTS_TODAY/$MAX_RESTARTS_PER_DAY today). Pre-restart logs: $snap"
    fi
  fi
  edge_check; daily_checks; save_state
}

# ----------------------------------------------------------------- entrypoints
case "${1:-run}" in
  run)        cycle ;;
  test-alert) send_telegram "${2:-test alert — install OK}" \
                && say "telegram OK" || { say "telegram FAILED"; exit 1; } ;;
  alert)      alert "${2:-unspecified alert}" ;;
  status)     load_state
              say "state: STATUS=$STATUS FAILS=$FAILS RESTARTS_TODAY=$RESTARTS_TODAY ($RESTARTS_DATE)"
              say "basic:  $(curl -m 5 -s -o /dev/null -w '%{http_code}' "$API/api/health" 2>/dev/null || echo 000)"
              say "strict: $(curl -m 5 -s "$API/api/health/strict" 2>/dev/null | head -c 300)" ;;
  reset-day)  load_state; FAILS=0; RESTARTS_TODAY=0; LAST_DOWN_ALERT=0; save_state; say "daily counters reset" ;;
  *)          say "unknown subcommand: $1"; exit 2 ;;
esac
