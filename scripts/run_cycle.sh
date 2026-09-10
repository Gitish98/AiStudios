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
    local code
    # Record the monitor's answer in the cron log. The ping used to be silent,
    # which meant "did the heartbeat reach healthchecks?" could only be answered
    # from inside the operator's account -- the one place the bot cannot look.
    if [ -n "${2:-}" ]; then
        code="$(curl -sS -m 15 --retry 3 -o /dev/null -w "%{http_code}" --data-binary @"$2" "$url" 2>/dev/null || echo "curl-failed")"
    else
        code="$(curl -sS -m 15 --retry 3 -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "curl-failed")"
    fi
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] heartbeat ping ${1:-success} -> ${code}"
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
AUDIT_LINE="$("$PY" cli.py audit --brief 2>&1)"; AUDIT_RC=$?
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
    # change $RC (an open question about the book is not a failed cycle), but a
    # HIGH or CRITICAL finding does flip the heartbeat to /fail below, because a
    # finding that only rides in a green ping's body is a finding nobody reads.
    echo "$AUDIT_LINE"
    # Delayed-data entitlement notices are counted by the cycle itself and
    # printed as one line (core/ib_noise.py). Anything ELSE that still prints
    # as an IB Error is unfiltered by design -- an unclassified code must stay
    # loud -- so census those separately.
    NOTICE_LINE="$(grep -E "^  IB notices:" "$LOG" | tail -1)"
    echo "${NOTICE_LINE:-  IB notices: (line absent -- cycle exited before the census)}"
    # Defense in depth for the census itself. The Python filter is bound to the
    # library's logger NAME; an upstream rename would leave it counting nothing
    # while the raw flood resumed -- and if this grep dropped those codes by
    # name regardless, the body would read "none" over 2,300 lines of them. An
    # alarm switched off must not look like an alarm that is clean, so compare
    # the raw log against the census and say so when they disagree.
    RAW_NOTICES="$(grep -cE "^Error (10089|10090|10091)," "$LOG" || true)"
    if [ "${RAW_NOTICES:-0}" -gt 0 ] && ! echo "$NOTICE_LINE" | grep -qE "1008[9]|1009[01]"; then
        echo "IB notices DISAGREE: raw log has ${RAW_NOTICES} delayed-data lines but the cycle censused none -- the notice filter is not seeing the library logger (renamed on upgrade?)"
    fi
    ERRS="$(grep -oE "^Error [0-9]+" "$LOG" | grep -vE "^Error (10089|10090|10091)$" | sort | uniq -c | sort -rn | head -4 | tr "\n" " ")"
    [ -n "$ERRS" ] && echo "ib_errors(unclassified): $ERRS"
} > "$SUMMARY" 2>&1

# Report to the monitor. /<n> marks failure on healthchecks.io. A clean cycle
# with an URGENT audit finding is reported as a failure too -- the cycle ran,
# but the book has a question the operator must see today, and an email is the
# only channel that reaches a phone. $RC itself is untouched.
# CRITICAL only: a HIGH finding can persist for days (a partial close waiting
# on a snapshot), and a check that is "down" for a week stops notifying about
# the real failure that happens inside that week. HIGH rides in the body.
PING_SUFFIX="$RC"
if [ "$RC" -eq 0 ] && [ "${AUDIT_RC:-0}" -ge 3 ]; then
    PING_SUFFIX="fail"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] audit is CRITICAL -> heartbeat reports failure"
fi
ping "$PING_SUFFIX" "$SUMMARY"
rm -f "$SUMMARY"
rm -f "$LOG"
exit "$RC"
