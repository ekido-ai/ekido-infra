#!/usr/bin/env bash
# Daily backup of the Base Sepolia coordinator: a consistent SQLite snapshot (online backup API, safe while
# the service runs) and an append-only copy of the content-addressed artifacts. Nothing is ever deleted here.
# The local copy protects against corruption and mistakes. If /etc/ekido/backup.env sets BACKUP_S3_BUCKET, each
# snapshot and any new artifacts are also copied to that S3 bucket, which protects against losing the disk.
# The upload never deletes anything in the bucket; the bucket's own lifecycle rules expire old copies.
set -euo pipefail
BACKUP_S3_BUCKET=""   # Amazon S3 bucket name
[ -f /etc/ekido/backup.env ] && . /etc/ekido/backup.env
SRC_DB=/var/lib/ekido-coordinator/jobs.db
SRC_ART=/var/lib/ekido-coordinator/artifacts
DEST=/var/backups/ekido
STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$DEST/db" "$DEST/artifacts"
python3 - "$SRC_DB" "$DEST/db/jobs-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
if ok != "ok":
    raise SystemExit(f"backup failed its integrity check: {ok}")
PY
gzip -9 "$DEST/db/jobs-$STAMP.db"
# Artifacts are named by their hash and never change, so copying only new files is enough.
cp -rn "$SRC_ART/." "$DEST/artifacts/" 2>/dev/null || true
echo "backup ok: $DEST/db/jobs-$STAMP.db.gz ($(du -sh "$DEST" | cut -f1) total)"
if [ -n "$BACKUP_S3_BUCKET" ]; then
  # Each snapshot name is unique (UTC timestamp), so nothing is overwritten. `sync` without --delete only adds
  # new artifacts, which are named by their hash and never change. The instance role must not allow s3:DeleteObject.
  aws s3 cp "$DEST/db/jobs-$STAMP.db.gz" "s3://$BACKUP_S3_BUCKET/db/"
  aws s3 sync "$DEST/artifacts" "s3://$BACKUP_S3_BUCKET/artifacts"
  echo "off-site ok: s3://$BACKUP_S3_BUCKET/db/jobs-$STAMP.db.gz"
fi
