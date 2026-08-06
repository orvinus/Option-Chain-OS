#!/usr/bin/env bash
# Wrapper so every compose command gets the right flags — plus deploy safety rails.
#
# WHY THIS EXISTS: with `-f docker/docker-compose.yml`, Compose treats docker/ as the
# project directory and looks for docker/.env — NOT the repo-root .env where the real
# settings live. Forget `--env-file .env` and compose either fails ("required variable
# POSTGRES_PASSWORD is missing a value") or, worse, silently starts with different
# values than the last deploy.
#
# Safety rails added after the 2026-08 outages:
#   * Container-recreating commands (default/up/down) snapshot the backend's logs
#     FIRST — a rebuild deletes the old container and its entire json-file log,
#     which is exactly how the 08-05 outage evidence was destroyed.
#   * A grace marker tells the oi-sentinel a deploy is in progress so it never
#     counts the legitimate ~60s gap as an outage.
#   * `up` (and the default) end with a HEALTH GATE: the deploy fails loudly if
#     the backend doesn't come up healthy, instead of exiting 0 into a broken
#     state that gets discovered the next morning.
#
# Usage:
#   scripts/deploy.sh                 # build + start + health gate (default)
#   scripts/deploy.sh ps
#   scripts/deploy.sh logs -f backend
#   scripts/deploy.sh restart backend # keeps the container => keeps its logs
#   scripts/deploy.sh logs-save       # manual log snapshot, then exit
set -euo pipefail

cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "ERROR: .env not found in $(pwd)"; exit 1; }

# Fail early with a clear message instead of a compose interpolation error.
for required in POSTGRES_PASSWORD; do
  grep -qE "^${required}=.+" .env || {
    echo "ERROR: $required is not set in .env"
    echo "  Generate one:  openssl rand -base64 32 | tr -d '/+=' | head -c 32"
    echo "  NOTE: on an EXISTING database you must ALTER USER postgres PASSWORD '<new>' FIRST,"
    echo "        otherwise the backend cannot authenticate."
    exit 1
  }
done

COMPOSE=(docker compose --env-file .env -f docker/docker-compose.yml)
DEPLOY_LOG_DIR=/var/log/oi-deploy
SENTINEL_STATE_DIR=/var/lib/oi-sentinel
API_BIND=$(grep -E '^API_BIND=' .env | cut -d= -f2- || true)
API="http://${API_BIND:-127.0.0.1:8000}"

snapshot_logs() {
  local tag="$1"
  mkdir -p "$DEPLOY_LOG_DIR" 2>/dev/null || return 0   # degrade quietly off-VPS
  local out="$DEPLOY_LOG_DIR/backend-${tag}-$(date +%Y%m%d-%H%M%S).log"
  if "${COMPOSE[@]}" logs --no-color --tail 4000 backend > "$out" 2>/dev/null; then
    echo "deploy: saved backend log snapshot -> $out"
  else
    rm -f "$out"
  fi
  find "$DEPLOY_LOG_DIR" -name 'backend-*' -mtime +30 -delete 2>/dev/null || true
}

in_watch_window() {
  local dow hm
  dow=$(TZ=Asia/Kolkata date +%u)   # 1..7
  hm=$(TZ=Asia/Kolkata date +%H%M)
  [[ $dow -le 5 && $hm -ge 0855 && $hm -le 1545 ]]
}

health_gate() {
  echo "deploy: waiting for the backend to answer /api/health ..."
  local deadline=$((SECONDS + 90))
  until curl -fsS -m 5 "$API/api/health" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "=============================================="
      echo " DEPLOY HEALTH GATE: FAIL (no /api/health in 90s)"
      echo " Inspect: scripts/deploy.sh logs --tail 100 backend"
      echo "=============================================="
      return 1
    fi
    sleep 3
  done
  echo "deploy: backend is answering."
  if in_watch_window; then
    echo "deploy: market hours — waiting for /api/health/strict to go green ..."
    deadline=$((SECONDS + 180))
    local code body
    while true; do
      body=$(curl -s -m 5 "$API/api/health/strict" || true)
      code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$API/api/health/strict" || echo 000)
      if [[ "$code" == "200" ]]; then
        break
      fi
      if [[ "$code" == "404" ]]; then
        echo "deploy: strict probe not in this build (404) — basic health accepted."
        break
      fi
      if (( SECONDS >= deadline )); then
        echo "=============================================="
        echo " DEPLOY HEALTH GATE: FAIL (strict still $code after 180s)"
        echo " Last body: $body"
        echo "=============================================="
        return 1
      fi
      sleep 5
    done
  fi
  echo "=============================================="
  echo " DEPLOY HEALTH GATE: PASS"
  echo "=============================================="
}

# ----- subcommand handling ---------------------------------------------------

if [[ "${1:-}" == "logs-save" ]]; then
  snapshot_logs manual
  exit 0
fi

[[ $# -eq 0 ]] && set -- up -d --build

RECREATES=0
GATE=0
case "${1:-}" in
  up)   RECREATES=1; GATE=1 ;;
  down) RECREATES=1 ;;
esac

if [[ $RECREATES -eq 1 ]]; then
  snapshot_logs predeploy
  # Grace marker for the oi-sentinel (harmless when the sentinel isn't installed).
  if mkdir -p "$SENTINEL_STATE_DIR" 2>/dev/null; then
    date +%s > "$SENTINEL_STATE_DIR/deploy-in-progress" 2>/dev/null || true
    trap 'rm -f "$SENTINEL_STATE_DIR/deploy-in-progress" 2>/dev/null || true' EXIT
  fi
fi

if [[ $RECREATES -eq 1 ]]; then
  # No exec: the EXIT trap must fire to clear the deploy marker.
  "${COMPOSE[@]}" "$@"
  if [[ $GATE -eq 1 ]]; then
    health_gate
  fi
else
  exec "${COMPOSE[@]}" "$@"
fi
