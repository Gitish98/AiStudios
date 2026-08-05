#!/usr/bin/env bash
# Cron wrapper for one paper cycle. Hardened for unattended operation:
#   - a HARD timeout so a hung IB market-data fetch can never wedge the box or
#     hold the data line indefinitely (chain fetches now stream + settle, so a
#     stall is plausible),
#   - a dead-man's-switch heartbeat: pings a monitor on BOTH success and failure,
#     carrying the exit code, so total silence (VM off, cron removed, cycle
#     killed before it can report) is itself an alert.
# Logs are handled by the crontab redirect (see docs/10 §7).
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
mkdir -p logs

# SINGLE-INSTANCE LOCK: two cycles at once fight over the one IB clientId, the
# one market-data session, and the SQLite store. A cron overrun meeting the next
# slot (cycles grow with the book) or a manual run beside the cron should skip
# loudly, not collide.
LOCK="$REPO_DIR/data/.cycle.lock"
mkdir -p "$REPO_DIR/data"
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] another cycle is already running — skipping (single-instance lock)"
    exit 0
fi

PY="$REPO_DIR/.venv/bin/python"
CYCLE_TIMEOUT="${CYCLE_TIMEOUT:-1800}"  # 30 min: cycles grow with the book (chains + per-leg marks); a stall still dies well before the next cron slot
# Optional heartbeat URL (healthchecks.io etc.). Read from a gitignored file so
# the URL — which is a shared secret — never enters the repo or the crontab.
HC_URL="$(cat "$REPO_DIR/.healthcheck_url" 2>/dev/null || true)"

ping() {   # ping <suffix> [body-file] — best-effort, never fails the script
    [ -n "$HC_URL" ] || return 0
    local url="$HC_URL"; [ -n "${1:-}" ] && url="$HC_URL/$1"
    if [ -n "${2:-}" ]; then
        curl -fsS -m 15 --retry 3 --data-binary @"$2" "$url" >/dev/null 2>&1 || true
    else
        curl -fsS -m 15 --retry 3 "$url" >/dev/null 2>&1 || true
    fi
}

ping start
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run-cycle starting (timeout ${CYCLE_TIMEOUT}s)"

LOG="$(mktemp)"
timeout --signal=TERM --kill-after=30 "$CYCLE_TIMEOUT" "$PY" cli.py run-cycle 2>&1 | tee "$LOG"
RC="${PIPESTATUS[0]}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run-cycle done (exit $RC)"
if [ "$RC" -eq 124 ]; then
    echo "  ✗ TIMED OUT after ${CYCLE_TIMEOUT}s — a fetch likely hung."
fi
# Report the exit code to the monitor. /<n> marks failure on healthchecks.io.
ping "$RC" "$LOG"
rm -f "$LOG"
exit "$RC"
