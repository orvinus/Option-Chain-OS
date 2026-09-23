#!/usr/bin/env bash
# oi-doctor — one command that verifies the ENTIRE reliability stack is armed.
#
#   sudo oi-doctor
#
# Prints PASS/WARN/FAIL per check; exits 1 if anything FAILs. Read-only except
# the Telegram test send. Run after install, after any reboot, and whenever
# you're unsure the safety net is actually live — an unarmed watchdog is
# indistinguishable from an armed one until the day it matters.
set -uo pipefail

OI_REPO="${OI_REPO:-/root/nifty-oi}"
[[ -f /etc/oi-sentinel.env ]] && . /etc/oi-sentinel.env
API_BIND=$(grep -E '^API_BIND=' "$OI_REPO/.env" 2>/dev/null | cut -d= -f2- || true)
API="http://${API_BIND:-127.0.0.1:8000}"

PASS=0; WARN=0; FAIL=0
ok()   { echo "PASS  $1"; PASS=$((PASS+1)); }
warn() { echo "WARN  $1"; WARN=$((WARN+1)); }
bad()  { echo "FAIL  $1"; FAIL=$((FAIL+1)); }

# 1. .env essentials
if [[ -f "$OI_REPO/.env" ]]; then
  ok ".env present at $OI_REPO"
  for k in POSTGRES_PASSWORD XTS_MD_APP_KEY XTS_MD_SECRET_KEY; do
    grep -qE "^$k=.+" "$OI_REPO/.env" && ok "$k set" || bad "$k missing in .env"
  done
  for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
    grep -qE "^$k=.+" "$OI_REPO/.env" && ok "$k set" || bad "$k missing — nothing can page you"
  done
  grep -qE "^RUN_MODE=live" "$OI_REPO/.env" && ok "RUN_MODE=live" || warn "RUN_MODE is not live (expected on prod)"
  grep -qE "^LOG_FORMAT=json" "$OI_REPO/.env" && ok "LOG_FORMAT=json (grep-able logs)" || warn "LOG_FORMAT not json — logs carry ANSI colors"
else
  bad ".env not found at $OI_REPO"
fi

# 2. containers + restart policies + log rotation
if docker info >/dev/null 2>&1; then
  ok "docker daemon up"
  for svc in timescaledb backend frontend; do
    id=$(docker compose --env-file "$OI_REPO/.env" -f "$OI_REPO/docker/docker-compose.yml" ps -q "$svc" 2>/dev/null | head -1)
    if [[ -n "$id" && "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" == "true" ]]; then
      ok "container $svc running"
      pol=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$id")
      [[ "$pol" == "unless-stopped" || "$pol" == "always" ]] && ok "$svc restart policy: $pol" || bad "$svc restart policy is '$pol'"
      sz=$(docker inspect -f '{{index .HostConfig.LogConfig.Config "max-size"}}' "$id" 2>/dev/null)
      [[ -n "$sz" ]] && ok "$svc log rotation: max-size=$sz" || warn "$svc has NO log rotation (recreate after the compose logging change)"
    else
      bad "container $svc NOT running"
    fi
  done
else
  bad "docker daemon unreachable"
fi

# 3. probes
code=$(curl -m 5 -s -o /dev/null -w '%{http_code}' "$API/api/health" 2>/dev/null || echo 000)
[[ "$code" == "200" ]] && ok "basic /api/health answers 200" || bad "basic health: $code"
scode=$(curl -m 5 -s -o /dev/null -w '%{http_code}' "$API/api/health/strict" 2>/dev/null || echo 000)
case "$scode" in
  200|503) ok "strict probe present (HTTP $scode)";;
  404)     warn "strict probe 404 — backend build predates it";;
  *)       bad "strict probe unreachable ($scode)";;
esac

# 4. timers + CRLF hygiene
for t in oi-sentinel.timer oi-backup.timer; do
  systemctl is-enabled "$t" >/dev/null 2>&1 && ok "$t enabled" || bad "$t not enabled (run scripts/install-sentinel.sh)"
done
systemctl is-active oi-sentinel.timer >/dev/null 2>&1 && ok "oi-sentinel.timer active" || bad "oi-sentinel.timer inactive"
for f in /etc/systemd/system/oi-sentinel.service /etc/systemd/system/oi-backup.timer /usr/local/bin/oi-sentinel; do
  if [[ -f "$f" ]]; then
    grep -q $'\r' "$f" && bad "$f contains CRLF (\\r) — reinstall from the git checkout" || ok "$(basename "$f") is CRLF-clean"
  fi
done
jr=$(journalctl -u oi-sentinel.service --since "-5 min" --no-pager 2>/dev/null | tail -1)
[[ -n "$jr" ]] && ok "sentinel ran within the last 5 min" || warn "no sentinel run in 5 min (fresh install? check list-timers)"

# 5. clock
timedatectl show -p NTPSynchronized 2>/dev/null | grep -q yes && ok "NTP synchronized" || warn "NTP not synchronized — token TTL math drifts"
echo "      (IST now: $(TZ=Asia/Kolkata date '+%a %H:%M') — sentinel window is Mon-Fri 08:55-15:45)"

# 6. telegram + dead-man
if /usr/local/bin/oi-sentinel test-alert "oi-doctor check $(date +%H:%M:%S)" >/dev/null 2>&1; then
  ok "telegram test message sent (check your phone)"
else
  bad "telegram send failed — token/chat id wrong or network blocked"
fi
[[ -n "${HEALTHCHECKS_URL:-}" ]] && ok "external dead-man ping configured" \
  || warn "HEALTHCHECKS_URL unset — a dead VPS/systemd/Telegram outage pages NOBODY (free: healthchecks.io)"

# 7. disk
du=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
if   (( du >= 85 )); then bad "disk ${du}% used"
elif (( du >= 70 )); then warn "disk ${du}% used"
else ok "disk ${du}% used"; fi

# 8. backups
newest=$(ls -t /var/backups/nifty-oi/oi_*.sql.gz 2>/dev/null | head -1)
if [[ -n "$newest" ]]; then
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
  (( age_h <= 26 )) && ok "newest backup ${age_h}h old" || bad "newest backup ${age_h}h old (>26h)"
  gzip -t "$newest" 2>/dev/null && ok "backup gzip integrity OK" || bad "newest backup fails gzip -t"
  grep -qE "^OI_BACKUP_REMOTE=.+" "$OI_REPO/.env" 2>/dev/null && ok "off-box backup target configured" \
    || warn "OI_BACKUP_REMOTE unset — backups die with this disk (the #1 open item)"
else
  bad "no backups in /var/backups/nifty-oi"
fi

# 9. firewall persistence
if iptables -S DOCKER-USER 2>/dev/null | grep -q -- "--dport 5432"; then
  ok "DOCKER-USER blocks 5432 live"
  [[ -f /etc/iptables/rules.v4 ]] && grep -q "5432" /etc/iptables/rules.v4 \
    && ok "iptables rules persisted" \
    || warn "iptables NOT persisted — run: netfilter-persistent save (after confirming SSH from a 2nd session)"
else
  warn "DOCKER-USER 5432 drop not found (harden-vps.sh not run?)"
fi

# 10. holiday calendar maintenance
hol="$OI_REPO/data/nse_holidays.json"
if [[ -f "$hol" ]]; then
  future=$(python3 - "$hol" <<'PY' 2>/dev/null
import json,sys,datetime
d=json.load(open(sys.argv[1]))
today=datetime.date.today().isoformat()
print(sum(1 for x in d.get("holidays",[]) if x>=today))
PY
)
  if [[ -n "$future" && "$future" -ge 4 ]]; then ok "holiday calendar has $future future dates"
  else warn "holiday calendar has only ${future:-0} future dates — fill from the NSE circular or expect capped false pages on holidays"; fi
else
  warn "data/nse_holidays.json missing"
fi

# 11. DB sanity
if docker compose --env-file "$OI_REPO/.env" -f "$OI_REPO/docker/docker-compose.yml" exec -T timescaledb pg_isready -U postgres >/dev/null 2>&1; then
  ok "postgres answers pg_isready"
else
  bad "postgres not ready"
fi

echo
echo "== $PASS PASS, $WARN WARN, $FAIL FAIL =="
exit $(( FAIL > 0 ? 1 : 0 ))
