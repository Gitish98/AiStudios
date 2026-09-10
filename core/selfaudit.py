"""selfaudit.py — deterministic invariant checks over our own records.

WHY THIS EXISTS
---------------
Every serious bug this project has shipped shares one shape: *something that
looked like a number but was actually an assumption*, or — worse — something
that looked like nothing at all. The system is already careful to represent
absence honestly (fills._usable refuses IB's 0.0; a missing measurement is
stored as NULL, never as a confident zero). That is the right call, and it has
prevented corruption.

But it created a second failure mode. A NULL is honest and it is also *silent*.
Trade #3 (SPY 763/762 x10, 2026-08-31) filled in full, the broker held the
average costs the whole time, and we wrote `entry_slip_ps: null` and moved on.
Nothing was wrong with the data. Nothing raised its hand either. It surfaced
eight days later only because a human happened to read the journal.

So this module is the missing half of the rule:

    Standing lesson #2 says absence must be UNREPRESENTABLE as a confident
    number. This adds: absence must also be UNIGNORABLE.

It asserts things we believe about our own book and reports what fails. It
touches no orders, changes no state, and reaches the broker only to read. It is
deterministic Python, not a model, because it guards money — and because an
agent does not survive a session while a cron does.

A finding is not necessarily a bug. It is a QUESTION that has gone unanswered
for longer than it should have, which on this system is the same alarm as
"0 signals": never read silence as health.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

# How long a filled position may go without its entry price being measured
# before we call it a problem. One cycle is normal (the backfill runs on the
# NEXT cycle after the fill); two is the honest grace period; beyond that the
# recovery path itself is failing.
UNMEASURED_GRACE_CYCLES = 2

# Below this fraction of cost dollars coming from real fills, the graduation
# gate's expectancy is mostly a model arguing with itself.
MIN_COST_MEASURED_PCT = 0.50

# Cycles run twice on each TRADING day. Staleness is therefore measured in
# missed trading days, never in wall-clock hours: a 30-hour rule reports a
# CRITICAL "the schedule is not running" every single weekend and every holiday
# on a perfectly healthy system, and an alarm that is wrong every Saturday is an
# alarm the operator learns to close without reading.
MAX_MISSED_TRADING_DAYS = 1

# Bounds on the staleness walk. Past these the answer is already "badly stale";
# counting further only burns exchange-calendar lookups on every cycle.
MAX_GAP_WALK_DAYS = 21
MAX_MISSED_REPORTED = 10

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Finding:
    code: str
    severity: str          # critical | high | medium | low
    summary: str           # one sentence, plain English
    evidence: dict[str, Any] = field(default_factory=dict)
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _rows(store, sql: str, args: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in store.conn.execute(sql, args)]
    except Exception:
        return []


def _scalar(store, sql: str, args: tuple = ()) -> Any:
    try:
        r = store.conn.execute(sql, args).fetchone()
        return r[0] if r else None
    except Exception:
        return None


# ─────────────────────────────── the checks ───────────────────────────────
# Each returns a list of Findings. They read; they never write.

def _check_unmeasured_entry_fills(store) -> list[Finding]:
    """An OPEN position whose entry price was never captured.

    This is the exact defect that motivated the module. It matters beyond
    bookkeeping: the entry price is the input to slippage, slippage is the
    measured half of the cost model, and the cost model is what tells us
    whether the strategy is worth real money. An unmeasured fill silently
    hands that judgement back to the model's guess."""
    out = []
    cycles = _cycles_since(store)
    for p in _rows(store, "SELECT * FROM positions WHERE status='open' "
                          "AND entry_fill_ps IS NULL"):
        # Count from when the position became OPEN, not from when the order was
        # placed. A limit entry can sit pending for many cycles and then fill;
        # measuring from placement spends the whole grace period before the
        # backfill has had a single attempt, and the alarm then fires on a
        # position that is behaving perfectly.
        n = cycles(_became_open_ts(store, p))
        if n is None or n < UNMEASURED_GRACE_CYCLES:
            continue
        out.append(Finding(
            code="unmeasured_entry_fill",
            severity="high",
            summary=(f"{p.get('underlying')} position #{p.get('id')} has been open "
                     f"for {n} cycles with no measured entry price — the backfill "
                     f"is not recovering it."),
            evidence={"position_id": p.get("id"), "underlying": p.get("underlying"),
                      "opened": p.get("opened_asof"), "cycles_open": n,
                      "contracts": p.get("contracts"),
                      "intended_ps": p.get("entry_credit_ps")},
            remedy="Check that the broker positions feed still returns this "
                   "structure's legs with a non-zero avg cost."))
    return out


def _became_open_ts(store, p: dict) -> Any:
    """When this position was first confirmed filled — the journal event if we
    have one, else the order timestamp as a conservative fallback."""
    ev = _scalar(store,
                 "SELECT ts FROM journal WHERE kind IN "
                 "('entry_filled','partial_fill_adopted','entry_fill_backfilled') "
                 "AND payload LIKE ? ORDER BY id LIMIT 1",
                 ('%"position_id": ' + str(int(p.get("id") or -1)) + '%',))
    return ev or p.get("opened_ts") or p.get("opened_asof")


def _check_unmeasured_exit_fills(store) -> list[Finding]:
    """A CLOSED position with no measured exit price.

    Unlike an entry, this one is genuinely unrecoverable: once the legs are
    gone from the account, the positions feed cannot reconstruct anything. So
    this finding is a post-mortem, not a to-do — it exists to keep the coverage
    number honest rather than to be fixed after the fact."""
    # A spread that EXPIRED worthless was never sold, so there is no exit price
    # to have measured and no cost to have missed (core/costs.py charges nothing
    # for it). Reporting it would leave the audit permanently dirty after the
    # strategy's most common winning outcome — and an audit that can never be
    # clean is an audit nobody reads, which defeats the entire module.
    rows = _rows(store, "SELECT * FROM positions WHERE status='closed' "
                        "AND exit_fill_ps IS NULL "
                        "AND COALESCE(exit_reason,'') <> 'expired'")
    if not rows:
        return []
    return [Finding(
        code="unmeasured_exit_fill",
        severity="medium",
        summary=(f"{len(rows)} closed position(s) have no measured EXIT price, so "
                 f"their exit cost is modeled rather than observed. This is not "
                 f"recoverable — the legs are gone."),
        evidence={"position_ids": [r.get("id") for r in rows],
                  "underlyings": sorted({str(r.get("underlying")) for r in rows})},
        remedy="Capture must happen at close time; verify the close finalizer "
               "reconstructs from the order feed before the legs disappear.")]


def _check_fill_mode_recorded(store) -> list[Finding]:
    """Every filled position should say whether it filled on paper or live.

    Without it, the day live fills start arriving there is no way to separate
    them from the optimistic paper ones in the same table — and the whole
    paper-optimism caveat becomes unenforceable."""
    rows = _rows(store, "SELECT id, underlying, status FROM positions "
                        "WHERE status IN ('open','closed') AND fill_mode IS NULL")
    if not rows:
        return []
    return [Finding(
        code="fill_mode_missing",
        severity="medium",
        summary=(f"{len(rows)} filled position(s) have no fill_mode, so paper and "
                 f"live fills cannot be told apart in the record."),
        evidence={"position_ids": [r.get("id") for r in rows]},
        remedy="fill_mode is set by record_entry_fill/record_exit_fill; a position "
               "that never got a measured fill never got a mode either.")]


def _check_max_loss_vs_fill(store) -> list[Finding]:
    """Book heat is computed from what we MEANT to pay.

    The risk gate necessarily approves on intent — the fill does not exist yet.
    But nothing rewrites max_loss afterwards, so a position that slipped is
    carried at less risk than it actually holds. Small per trade; it compounds
    across a book, and it always errs in the flattering direction."""
    out = []
    for p in _rows(store, "SELECT * FROM positions WHERE status='open' "
                          "AND entry_fill_ps IS NOT NULL"):
        try:
            n = int(p.get("contracts") or 0)
            intended = abs(float(p.get("entry_credit_ps") or 0.0))
            actual = abs(float(p.get("entry_fill_ps") or 0.0))
            booked = float(p.get("max_loss") or 0.0)
        except (TypeError, ValueError):
            continue
        if n <= 0 or actual <= 0 or booked <= 0:
            continue
        # Debit structures risk exactly what was paid; credit structures risk
        # width minus what was received. Either way an adverse fill raises it.
        if not int(p.get("is_credit") or 0):
            true_max = round(actual * 100.0 * n, 2)
        else:
            try:
                width = float(p.get("width") or 0.0)
            except (TypeError, ValueError):
                continue
            if width <= 0:
                continue
            true_max = round((width - actual) * 100.0 * n, 2)
        gap = round(true_max - booked, 2)
        if gap > max(1.0, 0.01 * booked):        # >1% and >$1 understated
            out.append(Finding(
                code="max_loss_understated",
                severity="low",
                summary=(f"{p.get('underlying')} #{p.get('id')} is booked at "
                         f"${booked:,.2f} max loss but the realized fill makes it "
                         f"${true_max:,.2f} — ${gap:,.2f} of risk is uncounted."),
                evidence={"position_id": p.get("id"), "booked_max_loss": booked,
                          "true_max_loss": true_max, "gap": gap,
                          "intended_ps": intended, "filled_ps": actual},
                remedy="Rescale max_loss from the realized fill once it is known, "
                       "the way adopt_partial_fill rescales for contract count."))
    return out


def _check_live_on_delayed_data(store) -> list[Finding]:
    """LIVE mode must be on real-time quotes. The go-live gate enforces this at
    arming time; this repeats the question every cycle, because a config edit
    after arming would otherwise go unnoticed until a fill came back wrong."""
    # "Live" here must mean ARMED NOW. The kv "mode" is written by run_cycle
    # from whichever adapter the last COMPLETED cycle was handed, so after an
    # operator stands down (`ais go-paper`, mode: paper) it stays "live" until
    # a cycle completes — and an off-hours skip or a degraded-sim refusal exits
    # before that line. The heartbeat runs `audit --brief` after every one of
    # those, so the stale kv alone would print a CRITICAL "live orders are being
    # priced off stale quotes" with no live adapter in existence. An audit that
    # cries wolf is an audit nobody reads. Armed = config says live AND the
    # dated arming phrase is set (what go-paper clears).
    try:
        from .config import load_config
        from .golive import ARMED_KV_KEY
        cfg = load_config()
        mode_cfg = str(getattr(cfg, "mode", "paper") or "paper").lower()
        armed = bool(store.get_kv(ARMED_KV_KEY))
    except Exception:
        return []
    if mode_cfg != "live" or not armed:
        return []
    mode = mode_cfg
    try:
        raw = cfg.brokers.get("ibkr_live_market_data_type")
    except Exception:
        raw = None
    try:
        ok = raw is not None and int(raw) == 1
    except (TypeError, ValueError):
        ok = False
    if ok:
        return []
    return [Finding(
        code="live_on_delayed_data",
        severity="critical",
        summary=("LIVE mode is armed but brokers.ibkr_live_market_data_type is "
                 f"{raw!r}, not 1 — live orders are being priced off stale quotes."),
        evidence={"mode": mode, "ibkr_live_market_data_type": raw},
        remedy="Run `ais go-paper` now; set ibkr_live_market_data_type: 1 and hold "
               "real-time entitlements before re-arming.")]


def _check_disk_space(store) -> list[Finding]:
    """SQLite fails to write when the disk is full, and it does not fail
    loudly from a cron. A book that stops recording is the worst kind of
    silence. Checked on the filesystem that holds the database."""
    import shutil
    try:
        path = None
        for _seq, _name, file in store.conn.execute("PRAGMA database_list"):
            if _name == "main" and file:
                path = file
                break
        if not path:
            return []
        from pathlib import Path
        free = shutil.disk_usage(Path(path).parent).free
    except Exception:
        return []
    gb = free / 1e9
    if gb >= 2.0:
        return []
    return [Finding(
        code="disk_low",
        severity="critical" if gb < 0.5 else "high",
        summary=f"Only {gb:.1f} GB free on the disk holding the books — SQLite "
                "will stop recording without warning when it hits zero.",
        evidence={"free_gb": round(gb, 2)},
        remedy="Free space (logs/, data/backups/) or resize the volume before the next cycle.")]


def _check_unbooked_partial_closes(store) -> list[Finding]:
    """A partial close whose filled units were NOT booked — no fill price was
    recoverable, or the report disagreed with the broker's book — is realized
    P&L missing from every metric. It used to be a journal row nobody read.
    Loud for 15 days: long enough to recover it from a snapshot, not forever."""
    from datetime import timedelta
    since = (_now() - timedelta(days=15)).isoformat()
    rows = _rows(store, "SELECT ts, payload FROM journal WHERE kind='partial_close_adopted' "
                        "AND ts >= ? ORDER BY id DESC", (since,))
    if not rows:
        return []
    items = []
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            p = {}
        items.append({"ts": str(r["ts"])[:16], "position_id": p.get("position_id"),
                      "closed_units": (int(p.get("was") or 0) - int(p.get("remaining") or 0)),
                      "executions_qty": p.get("executions_qty")})
    units = sum(i["closed_units"] for i in items)
    return [Finding(
        code="unbooked_partial_close",
        severity="high",
        summary=(f"{len(items)} partial close(s) totalling {units} contract(s) were resized "
                 f"but NOT booked — their realized P&L is missing from every metric."),
        evidence={"items": items[:5]},
        remedy="If an executions_snapshot for that day exists, the finalizer can book it on "
               "the next cycle; otherwise the price is gone and the record must say so.")]


def _check_cycle_freshness(store) -> list[Finding]:
    """The newest cycle should be recent. Everything downstream reads as current
    whether or not the data behind it still is."""
    ts = _scalar(store, "SELECT ts FROM journal WHERE kind='cycle_end' "
                        "ORDER BY id DESC LIMIT 1")
    d = _to_dt(ts)
    if d is None:
        return [Finding(code="no_cycles", severity="high",
                        summary="No completed cycle has ever been recorded.",
                        evidence={},
                        remedy="Check the cron and the run lock.")]
    missed = _missed_trading_days(d)
    if missed < MAX_MISSED_TRADING_DAYS:
        return []
    age = (_now() - d).total_seconds() / 3600.0
    return [Finding(
        code="stale_cycles",
        severity="critical",
        summary=(f"{missed} trading day(s) have passed with no completed cycle "
                 f"(last was {age:.0f}h ago) — the schedule is not running, and "
                 f"every figure on the dashboard is stale while still looking "
                 f"current."),
        evidence={"last_cycle_ts": ts, "missed_trading_days": missed,
                  "age_hours": round(age, 1)},
        remedy="ssh to the VM: check the cron, the Gateway, and the cycle lock.")]


def _missed_trading_days(last: datetime) -> int:
    """Trading days that came and went since `last` with no cycle.

    Weekends and holidays are not missed days — nothing was scheduled then. Today
    counts only once the session has closed, so a check run at 09:00 ET before the
    10:00 cron does not accuse the cron of being dead."""
    from datetime import timedelta
    try:
        from .market_calendar import is_trading_day
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        return 0                       # cannot tell -> do not cry wolf
    now_et = _now().astimezone(et)
    last_et = last.astimezone(et)
    gap = (now_et.date() - last_et.date()).days
    if gap > MAX_GAP_WALK_DAYS:
        # Weeks of silence needs no day-by-day proof, and enumerating it is not
        # free: is_trading_day consults the exchange calendar (~135ms a call), so
        # walking a stale store's whole history would cost seconds on every cycle
        # AND every dashboard build. The alarm is already maximally loud.
        return MAX_MISSED_REPORTED
    missed, day = 0, last_et.date() + timedelta(days=1)
    while day < now_et.date() and missed < MAX_MISSED_REPORTED:
        if is_trading_day(day):
            missed += 1
        day += timedelta(days=1)
    # Today is not a "missed" day until it is actually over.
    if (day == now_et.date() and missed < MAX_MISSED_REPORTED
            and is_trading_day(day) and now_et.hour >= 16):
        missed += 1
    return missed


def _check_persistent_drift(store) -> list[Finding]:
    """Reconcile drift that did not clear.

    One drift event is routine: an order fills between placement and reconcile,
    so the leg is at the broker before the row is marked open. It must be GONE
    by the next cycle. Drift that survives a cycle means our book and the
    broker's disagree about what we own, which is the precondition for every
    assignment surprise."""
    last_end = _scalar(store, "SELECT id FROM journal WHERE kind='cycle_end' "
                              "ORDER BY id DESC LIMIT 1")
    if last_end is None:
        return []
    prev_end = _scalar(store, "SELECT id FROM journal WHERE kind='cycle_end' "
                              "AND id < ? ORDER BY id DESC LIMIT 1", (last_end,))
    if prev_end is None:
        return []
    # Drift in the newest cycle ALONE is routine and expected: an entry order
    # fills between placement and the end-of-cycle reconcile, so the broker holds
    # a leg our row will not call 'open' until the NEXT cycle finalizes it. EVERY
    # entry produces it. The finding is drift that SURVIVED a cycle boundary —
    # that is where our book and the broker's genuinely disagree, which is the
    # precondition for every assignment surprise.
    rows = _rows(store, "SELECT ts, payload FROM journal WHERE kind='reconcile_drift' "
                        "AND id > ? AND id <= ?", (prev_end, last_end))
    if not rows:
        return []
    older = _scalar(store, "SELECT id FROM journal WHERE kind='cycle_end' "
                           "AND id < ? ORDER BY id DESC LIMIT 1", (prev_end,))
    if older is None:
        return []                      # not enough history to say it persisted
    before = _rows(store, "SELECT ts FROM journal WHERE kind='reconcile_drift' "
                          "AND id > ? AND id <= ?", (older, prev_end))
    if not before:
        return []                      # first cycle it appeared -> give it one
    for _id in (last_end, prev_end):
        pay = _scalar(store, "SELECT payload FROM journal WHERE id = ?", (_id,))
        try:
            if json.loads(pay or "{}").get("reconcile_ok") is True:
                return []              # cleared by the end of one of them
        except Exception:
            pass
    return [Finding(
        code="reconcile_drift_unresolved",
        severity="high",
        summary="Our book and the broker's have disagreed about what we hold for "
                "two consecutive cycles — this is no longer a fill in flight.",
        evidence={"events": [r.get("payload") for r in rows][:3]},
        remedy="Run `ais reconcile`. If a leg is held at the broker and untracked "
               "here, freeze the underlying before anything else trades it.")]


def _check_measurement_coverage(store) -> list[Finding]:
    """How much of the cost figure is observed rather than assumed.

    The graduation gate turns costs into a go/no-go on real money. If most of
    that number is the model's own estimate, the gate is largely marking its own
    homework."""
    try:
        from .performance import compute_metrics
        from .costs import CostModel
        from .config import load_config
        try:
            costs = CostModel.from_config(load_config())
        except Exception:
            costs = CostModel()
        closed = _rows(store, "SELECT * FROM positions WHERE status='closed'")
        if not closed:
            return []
        days = sorted({p.get("closed_asof") for p in closed if p.get("closed_asof")})
        m = compute_metrics(closed, days, costs)
    except Exception:
        return []
    pct = float(m.get("cost_measured_pct") or 0.0)
    if pct >= MIN_COST_MEASURED_PCT:
        return []
    return [Finding(
        code="low_cost_measurement",
        severity="medium",
        summary=(f"Only {pct * 100:.0f}% of the cost figure comes from measured "
                 f"fills; the rest is the model's estimate, and the modeled part "
                 f"is the optimistic part."),
        evidence={"cost_measured_pct": pct, "floor": MIN_COST_MEASURED_PCT,
                  "trades": m.get("trades")},
        remedy="Every missed capture lowers this. Chase unmeasured_entry_fill and "
               "unmeasured_exit_fill findings first.")]


def _check_orphan_pending(store) -> list[Finding]:
    """A row stuck 'pending' across many cycles.

    Pending consumes risk budget but is not managed and not expected at the
    broker. A row that never resolves is budget spent on nothing — or a real
    position nobody is watching."""
    out = []
    cycles = _cycles_since(store)
    for p in _rows(store, "SELECT * FROM positions WHERE status='pending'"):
        n = cycles(p.get("opened_ts") or p.get("opened_asof"))
        if n is None or n < 3:
            continue
        out.append(Finding(
            code="stuck_pending",
            severity="high",
            summary=(f"{p.get('underlying')} #{p.get('id')} has been 'pending' for "
                     f"{n} cycles — it is consuming risk budget while being "
                     f"neither managed nor expected at the broker."),
            evidence={"position_id": p.get("id"), "cycles_pending": n,
                      "opened": p.get("opened_asof")},
            remedy="The finalizer resolves pending against the broker's positions; "
                   "if it cannot see them, the connection is the problem."))
    return out


def _cycles_since(store):
    """A closure counting completed cycles since a timestamp — the natural clock
    for this system. Wall-clock days mislead across weekends and holidays."""
    ends = [_to_dt(r["ts"]) for r in
            _rows(store, "SELECT ts FROM journal WHERE kind='cycle_end' ORDER BY id")]
    ends = [d for d in ends if d is not None]

    def count(since: Any) -> Optional[int]:
        d = _to_dt(since)
        if d is None:
            return None
        return sum(1 for e in ends if e > d)
    return count


CHECKS = (
    _check_live_on_delayed_data,
    _check_disk_space,
    _check_unbooked_partial_closes,
    _check_cycle_freshness,
    _check_persistent_drift,
    _check_unmeasured_entry_fills,
    _check_orphan_pending,
    _check_unmeasured_exit_fills,
    _check_fill_mode_recorded,
    _check_measurement_coverage,
    _check_max_loss_vs_fill,
)


def run_self_audit(store) -> list[Finding]:
    """Run every invariant check. Read-only; never raises out of a single check.

    A failing CHECK must never break a cycle — the auditor is a smoke detector,
    not load-bearing wiring."""
    found: list[Finding] = []
    for chk in CHECKS:
        try:
            found.extend(chk(store) or [])
        except Exception as e:                        # pragma: no cover - defensive
            found.append(Finding(
                code="audit_check_failed", severity="medium",
                summary=f"Self-audit check {getattr(chk, '__name__', '?')} raised: {e}",
                evidence={"check": getattr(chk, "__name__", "?")},
                remedy="The auditor itself is broken; fix it before trusting a "
                       "clean audit."))
    found.sort(key=lambda f: (SEV_ORDER.get(f.severity, 9), f.code))
    return found


def audit_payload(findings: list[Finding]) -> dict[str, Any]:
    """The shape journaled each cycle and read by the dashboard."""
    return {
        "count": len(findings),
        "worst": findings[0].severity if findings else None,
        "codes": [f.code for f in findings],
        "findings": [f.as_dict() for f in findings],
    }


def journal_self_audit(store, ts: Optional[str] = None) -> list[Finding]:
    """Run the audit and record it. Called at the end of every cycle.

    A CLEAN audit is journaled too. An alarm that only writes when it fires
    cannot be distinguished from an alarm that is switched off."""
    findings = run_self_audit(store)
    stamp = ts or _now().isoformat()
    try:
        store.append(stamp, "self_audit", audit_payload(findings))
    except Exception:
        pass
    return findings
