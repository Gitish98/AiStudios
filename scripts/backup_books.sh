#!/usr/bin/env bash
# Nightly backup of the books (data/portfolio.db).
#
# WHY: the store is the entire record -- every fill, every measurement, every
# audit -- and until 2026-09-09 nothing copied it anywhere. A VM failure would
# have lost every measurement since July. The DB is ~240 KB; there is no excuse.
#
# On-box: a CONSISTENT snapshot via SQLite's online backup API (safe under WAL,
# unlike cp), dated, kept for 14 days. Off-box: if the operator has written an
# rclone remote path into ~/.aistudios/backup_remote (e.g. "gdrive:aistudios"),
# the newest snapshot is copied there too. The off-box hop is the one that
# survives losing the VM; it needs a destination only the operator can supply.
set -uo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="${AIS_PY:-$REPO_DIR/.venv/bin/python}"
if [ ! -x "$PY" ]; then
    if command -v python3 >/dev/null 2>&1 && python3 -c "import sqlite3" >/dev/null 2>&1; then PY="python3";
    elif command -v python >/dev/null 2>&1; then PY="python";
    else echo "backup FAILED: no python interpreter found"; exit 1; fi
fi
DEST="$REPO_DIR/data/backups"; mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%d-%H%M)"
OUT="$DEST/portfolio-$STAMP.db"

"$PY" - "$REPO_DIR/data/portfolio.db" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)          # online backup API: consistent under WAL, no lock fight
d.close(); s.close()
PY
if [ ! -s "$OUT" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup FAILED: $OUT missing or empty"
    exit 1
fi
# verify the copy opens and has our tables before trusting it
"$PY" - "$OUT" <<'PY' || { echo "backup FAILED integrity check"; exit 1; }
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
n = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
j = c.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
print(f"  verified: {n} positions, {j} journal rows")
PY
# rotate: keep the newest 14
ls -1t "$DEST"/portfolio-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup OK -> $OUT ($(wc -c < "$OUT") bytes)"

# off-box, if a destination has been provided
REMOTE_FILE="$HOME/.aistudios/backup_remote"
if [ -s "$REMOTE_FILE" ] && command -v rclone >/dev/null 2>&1; then
    REMOTE="$(cat "$REMOTE_FILE")"
    if rclone copy "$OUT" "$REMOTE" 2>/dev/null; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] off-box copy OK -> $REMOTE"
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] off-box copy FAILED -> $REMOTE"
    fi
else
    echo "  (no off-box destination: write an rclone remote path to $REMOTE_FILE)"
fi
