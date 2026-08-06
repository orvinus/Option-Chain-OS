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

echo "== installing systemd units =="
install -m 644 "$REPO"/scripts/systemd/*.service "$REPO"/scripts/systemd/*.timer /etc/systemd/system/

echo "== dirs =="
mkdir -p /var/lib/oi-sentinel /var/log/oi-sentinel /var/log/oi-deploy /var/backups/nifty-oi
chmod 700 /var/backups/nifty-oi

echo "== enabling =="
systemctl daemon-reload
systemctl enable --now oi-sentinel.timer oi-backup.timer
systemctl enable docker >/dev/null 2>&1 || true

echo "== verify =="
systemctl list-timers 'oi-*' --no-pager || true
echo
echo "Next steps:"
echo "  1. Put TELEGRAM_BOT_TOKEN= and TELEGRAM_CHAT_ID= in $REPO/.env  (BotFather -> new bot; message it once; chat id via https://api.telegram.org/bot<TOKEN>/getUpdates)"
echo "  2. oi-sentinel test-alert 'install OK'"
echo "  3. Optional external dead-man: echo 'HEALTHCHECKS_URL=https://hc-ping.com/<uuid>' > /etc/oi-sentinel.env && chmod 600 /etc/oi-sentinel.env"
echo "  4. oi-doctor   # every check should PASS/WARN, none FAIL"
