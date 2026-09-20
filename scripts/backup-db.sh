#!/usr/bin/env bash
# Nightly TimescaleDB dump with retention. Safe to re-run.
#
# Install: scripts/install-sentinel.sh puts this at /usr/local/bin/oi-backup and
# enables oi-backup.timer (02:30 IST daily, OnFailure pages via Telegram). The
# oi-sentinel additionally alerts when the newest dump is >26h old.
#
# A dump ON THE SAME BOX only protects against bad deploys and accidental
# deletes, not disk loss or ransomware. Set OI_BACKUP_REMOTE in the repo .env
# (an rsync target, e.g. user@host:/backups/nifty-oi/) for the off-box copy.
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

# Off-box copy — a local-only backup dies with the disk. Warn-but-succeed: the
# local dump above is good, and the sentinel's backup-age check + this unit's
# OnFailure pager cover the alerting.
REMOTE=$(grep -E '^OI_BACKUP_REMOTE=' "$REPO/.env" 2>/dev/null | cut -d= -f2- || true)
if [[ -n "$REMOTE" ]]; then
  if rsync -az "$DEST/" "$REMOTE" 2>/dev/null; then
    echo "$(date -Is) off-box copy OK -> $REMOTE"
  else
    echo "$(date -Is) WARN: off-box copy to $REMOTE failed (local dump is fine)"
  fi
else
  echo "$(date -Is) NOTE: OI_BACKUP_REMOTE unset — no off-box copy (dies with this disk)"
fi

# Restore:
#   gunzip -c /var/backups/nifty-oi/oi_YYYY-MM-DD_HHMM.sql.gz \
#     | docker exec -i -e PGPASSWORD="$PW" <container> psql -U postgres -d oi
#
# Off-box copy (do this — a local-only backup dies with the disk), e.g.:
#   rsync -az "$DEST/" user@another-host:/backups/nifty-oi/
