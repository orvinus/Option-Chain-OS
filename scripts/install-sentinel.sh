#!/usr/bin/env bash
# One-command install of the OI ops layer on the VPS. Idempotent — re-run freely.
#
#   cd /root/nifty-oi && sudo bash scripts/install-sentinel.sh
#
# Files are installed FROM THE GIT CHECKOUT (never pasted over SSH — Windows
# clipboards inject \r characters that break systemd units in confusing ways;
# .gitattributes pins these files to LF).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "== installing scripts =="
install -m 755 "$REPO/scripts/oi-sentinel.sh" /usr/local/bin/oi-sentinel
install -m 755 "$REPO/scripts/oi-doctor.sh"   /usr/local/bin/oi-doctor
install -m 755 "$REPO/scripts/backup-db.sh"   /usr/local/bin/oi-backup
# The nightly archive top-up. Installed as a unit (not a hand-added crontab
# line) so OnFailure=oi-alert@ can page when the archive falls behind.
install -m 755 "$REPO/scripts/nightly_topup_vps.sh" /usr/local/bin/oi-topup

echo "== installing systemd units =="
install -m 644 "$REPO"/scripts/systemd/*.service "$REPO"/scripts/systemd/*.timer /etc/systemd/system/

echo "== dirs =="
mkdir -p /var/lib/oi-sentinel /var/log/oi-sentinel /var/log/oi-deploy /var/backups/nifty-oi
chmod 700 /var/backups/nifty-oi

echo "== enabling =="
systemctl daemon-reload
systemctl enable --now oi-sentinel.timer oi-backup.timer
# The top-up only makes sense where the vendor is actually reachable.
# On a dev box without credentials, skip it rather than enable a timer
# that will fail nightly and train the operator to ignore pages.
if grep -qE '^TRUEDATA_USER=.+' "$REPO/.env" 2>/dev/null; then
  systemctl enable --now oi-topup.timer
  echo "   oi-topup.timer enabled (17:35 / 21:00 / 06:30 IST)"
else
  echo "   skipping oi-topup.timer — no TRUEDATA_USER in .env (correct for a dev box)"
fi
systemctl enable docker >/dev/null 2>&1 || true

# WARP SOCKS relay — only where WARP is actually installed (i.e. the VPS, whose
# IP TrueData's edge drops). Without it the containerised backend cannot reach
# the vendor at all: WARP binds the HOST's 127.0.0.1, and inside the container
# that address is the container's own loopback. Idempotent and safe to re-run.
if systemctl list-unit-files 2>/dev/null | grep -q '^warp-svc\.service'; then
  echo "== WARP detected: enabling the SOCKS relay for the docker bridge =="
  if ! command -v socat >/dev/null 2>&1; then
    echo "   installing socat..."
    apt-get install -y socat >/dev/null 2>&1 || echo "   WARN: could not install socat — relay will fail to start"
  fi
  systemctl enable --now warp-socks-bridge.service || true
  # Prove it from INSIDE the container, which is the only place that matters.
  cid=$(docker ps --filter 'name=backend' --format '{{.Names}}' | head -1)
  if [ -n "$cid" ]; then
    code=$(docker exec "$cid" curl -s --max-time 10 --socks5-hostname warp-proxy:40000 \
             -o /dev/null -w '%{http_code}' https://auth.truedata.in 2>/dev/null || echo 000)
    if [ "$code" = "000" ] || [ -z "$code" ]; then
      echo "   FAIL: container cannot reach TrueData through warp-proxy:40000 (got '$code')"
      echo "         check: systemctl status warp-socks-bridge; warp-cli --accept-tos status"
    else
      echo "   OK: container reached TrueData through the relay (HTTP $code)"
    fi
  fi
else
  echo "== no warp-svc on this host: skipping the SOCKS relay (correct for a dev box) =="
fi

echo "== verify =="
systemctl list-timers 'oi-*' --no-pager || true
echo
echo "Next steps:"
echo "  1. Put TELEGRAM_BOT_TOKEN= and TELEGRAM_CHAT_ID= in $REPO/.env  (BotFather -> new bot; message it once; chat id via https://api.telegram.org/bot<TOKEN>/getUpdates)"
echo "  2. oi-sentinel test-alert 'install OK'"
echo "  3. Optional external dead-man: echo 'HEALTHCHECKS_URL=https://hc-ping.com/<uuid>' > /etc/oi-sentinel.env && chmod 600 /etc/oi-sentinel.env"
echo "  4. oi-doctor   # every check should PASS/WARN, none FAIL"
