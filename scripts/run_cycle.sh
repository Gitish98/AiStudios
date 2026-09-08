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
# ── Build the monitor's body: a SUMMARY, never the raw log. ──
# Measured on a real cycle, $LOG is ~1.1 MB and 3,237 lines, of which 2,330 are a
# single repeated IB market-data warning. healthchecks.io caps a ping body at
# ~100 KB and keeps the HEAD, so posting $LOG would have shipped a megabyte of
# noise per cycle and truncated away the only part that matters -- the result and
# the audit, which are at the TAIL. An alert nobody can read is the same failure
# as no alert at all.
#
# It is also deliberately money-free. This body leaves the VM for a third party;
# "go look" is as actionable as publishing equity and P&L, and safer.
SUMMARY="$(mktemp)"
{
    echo "cycle $(date -u +%Y-%m-%dT%H:%M:%SZ) exit=$RC"
    [ "$RC" -eq 124 ] && echo "TIMED OUT after ${CYCLE_TIMEOUT}s"
    # The cycle's own verdict lines, not the chain chatter.
    grep -E "^  (Signals generated|Placed|Rejected|Management|Daily P&L)" "$LOG" | tail -6
    grep -E "Skipping cycle|CYCLE FAILED|reconcile|drift" "$LOG" | tail -4
    # The book's own invariants. A cycle can SUCCEED while the records it
    # produced are incoherent -- an unmeasured fill is exactly that -- and that
    # question has to leave the VM, not just land in the journal. It does NOT
    # change $RC: an open question about the book is not a failed cycle, and
    # conflating them makes a real cycle failure harder to see.
    "$PY" cli.py audit --brief 2>&1 || true
    # An IB error census. 2,330 occurrences of one code is a fact about the
    # day worth seeing; 2,330 copies of the line is not.
    ERRS="$(grep -oE "Error [0-9]+" "$LOG" | sort | uniq -c | sort -rn | head -4 | tr "\n" " ")"
    [ -n "$ERRS" ] && echo "ib_errors: $ERRS"
} > "$SUMMARY" 2>&1

# Report the exit code to the monitor. /<n> marks failure on healthchecks.io.
ping "$RC" "$SUMMARY"
rm -f "$SUMMARY"
rm -f "$LOG"
exit "$RC"
