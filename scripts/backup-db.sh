#!/usr/bin/env bash
# Nightly TimescaleDB dump with retention. Safe to re-run; designed for cron.
#
# Install (runs 02:30 IST daily, before the pre-open window):
#   sudo cp scripts/backup-db.sh /usr/local/bin/oi-backup && sudo chmod +x /usr/local/bin/oi-backup
#   ( crontab -l 2>/dev/null; echo "30 2 * * * /usr/local/bin/oi-backup >>/var/log/oi-backup.log 2>&1" ) | crontab -
#
# A dump ON THE SAME BOX only protects against bad deploys and accidental
# deletes, not disk loss or ransomware. Copy DEST off-box as well (see the
# note at the end of this file).
set -euo pipefail

REPO="${OI_REPO:-/root/nifty-oi}"
DEST="${OI_BACKUP_DIR:-/var/backups/nifty-oi}"
KEEP_DAYS="${OI_BACKUP_KEEP_DAYS:-14}"
DB_NAME="${OI_DB_NAME:-oi}"

mkdir -p "$DEST"
chmod 700 "$DEST"

# Find the running Timescale container regardless of the compose project name.
CID="$(docker ps --filter 'ancestor=timescale/timescaledb:latest-pg16' --format '{{.ID}}' | head -1)"
[[ -n "$CID" ]] || CID="$(docker ps --format '{{.ID}} {{.Names}}' | awk '/timescale/{print $1; exit}')"
[[ -n "$CID" ]] || { echo "$(date -Is) ERROR: no timescaledb container running"; exit 1; }

# Password comes from the repo .env (never hardcode it here).
PW=""
[[ -f "$REPO/.env" ]] && PW="$(grep -E '^POSTGRES_PASSWORD=' "$REPO/.env" | tail -1 | cut -d= -f2- || true)"

STAMP="$(date +%F_%H%M)"
OUT="$DEST/oi_${STAMP}.sql.gz"
TMP="$OUT.partial"

echo "$(date -Is) starting dump -> $OUT"
# Write to .partial first so an interrupted run never leaves a truncated file
# that looks like a valid backup.
if docker exec -e PGPASSWORD="$PW" "$CID" pg_dump -U postgres --no-owner "$DB_NAME" 2>/dev/null | gzip -9 > "$TMP"; then
  # gzip -t catches truncation; a 0-byte or corrupt dump is worse than none.
  if [[ -s "$TMP" ]] && gzip -t "$TMP" 2>/dev/null; then
    mv "$TMP" "$OUT"
    chmod 600 "$OUT"
    echo "$(date -Is) OK $(du -h "$OUT" | cut -f1) $OUT"
  else
    rm -f "$TMP"; echo "$(date -Is) ERROR: dump was empty or corrupt"; exit 1
  fi
else
  rm -f "$TMP"; echo "$(date -Is) ERROR: pg_dump failed"; exit 1
fi

# Retention — only delete AFTER a good dump exists, never before.
find "$DEST" -name 'oi_*.sql.gz' -mtime "+$KEEP_DAYS" -print -delete
echo "$(date -Is) done. $(ls -1 "$DEST"/oi_*.sql.gz 2>/dev/null | wc -l) backup(s) retained."

# Restore:
#   gunzip -c /var/backups/nifty-oi/oi_YYYY-MM-DD_HHMM.sql.gz \
#     | docker exec -i -e PGPASSWORD="$PW" <container> psql -U postgres -d oi
#
# Off-box copy (do this — a local-only backup dies with the disk), e.g.:
#   rsync -az "$DEST/" user@another-host:/backups/nifty-oi/
