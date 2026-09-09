"""ramp.py — the live ramp's EVIDENCE engine.

WHY THIS EXISTS
---------------
docs/05 §1.3 describes a staged ramp: tier 1 ($250/position) -> tier 2 -> tier 3,
each advanced by the human editing config "after a clean review period", where a
guardrail breach "resets the clean-day counter to zero". `core.golive.
ramp_advancement_status` implements the verdict. On 2026-09-08 it was found to
have NO CALLER: nothing computed `clean_days`, nothing computed live-only
metrics, and `ready_to_advance` was surfaced nowhere. The ramp — the mechanism
by which a $250 validation trade becomes "the money" — had a documented evidence
standard and no evidence. This module is the producer that was missing.

It READS the journal and the positions table; it writes nothing and promotes
nothing. The human still edits the tier — but the go-live gate (#7,
`_check_graduation`) now refuses a tier the evidence has not earned.

THE FIRST VERSION OVERSTATED THE EVIDENCE — a 20-agent preflight review found
fourteen ways, all in the optimistic direction, before it shipped. Each is now a
rule below and a test in tests/test_ramp.py:

  * Evidence is attributed PER DAY to the tier that was ARMED that day. Arming is
    date-bound (`go_live_armed` carries the date and the tier), so the journal
    holds a per-day tier record. Ten clean days run at tier 1 after a tier-2
    kill are tier-1 evidence, not tier-2's.
  * A day counts only if every live cycle on it COMPLETED (a `cycle_end` follows
    the `cycle_start`). A cycle killed by the cron timeout journals neither an
    end nor a fatal; it is not a clean day.
  * A day with the kill switch ENGAGED, or any underlying FROZEN, at the time a
    live cycle started is not clean. Ten days of the system being switched off
    are not ten days of clean operation.
  * A cycle whose `asof` differs from the ET date of its own timestamp was a
    replay (`--asof`) and is not a live day at all.
  * Live metrics use the CONFIGURED cost model — the same one the paper half of
    gate #7 and `cli.py performance` use — never defaults.
  * A journal that cannot be read is EVIDENCE UNAVAILABLE, reported as such,
    never "zero breaches". Standing lesson #2: absence must be unrepresentable
    as a confident number.

WHAT COUNTS AS A BREACH
-----------------------
From what is actually journaled: `kill_switch` with `engaged: true` (a
disengage is a clear); `underlying_frozen` (the assignment signature);
`cycle_fatal`; and PERSISTENT reconcile drift — drift that survived a cycle
boundary. Routine one-cycle drift is NOT a breach: every entry produces one
cycle of `untracked_at_broker` by design (the order fills between placement and
the end-of-cycle reconcile), and counting it would reset the clock on every
trade, making tier 1 a room with no door.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

LIVE_BROKER_NAME = "ibkr_live"
BREACH_KINDS = frozenset({"kill_switch", "underlying_frozen", "cycle_fatal"})
_STATE_KINDS = ("cycle_start", "cycle_end", "kill_switch", "underlying_frozen",
                "frozen", "underlying_unfrozen", "unfreeze")


class EvidenceUnavailable(RuntimeError):
    """The journal or cost model could not be read. Callers must treat this as
    'no evidence', never as 'nothing happened'."""


def _rows(store, sql: str, args: tuple = ()) -> list:
    try:
        return list(store.conn.execute(sql, args).fetchall())
    except Exception as e:                       # noqa: BLE001 — surfaced, not swallowed
        raise EvidenceUnavailable(f"journal unreadable: {e}") from e


def _payload(raw: Any) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _et_date(ts: Any) -> Optional[str]:
    """The Eastern trading date of an ISO timestamp (naive => UTC)."""
    if not ts:
        return None
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return None


def _is_breach(kind: str, payload: dict) -> bool:
    if kind == "kill_switch":
        return bool(payload.get("engaged", True))    # ambiguous => not clean
    return kind in BREACH_KINDS


# ─────────────────────────────── tier timeline ───────────────────────────────

def armed_tier_by_date(store) -> dict[str, int]:
    """date -> tier armed that day. Arming is date-bound (the confirmation phrase
    carries the date), so `go_live_armed` rows ARE the per-day tier record. The
    last arming on a date wins."""
    out: dict[str, int] = {}
    for ts, raw in _rows(store, "SELECT ts, payload FROM journal "
                                "WHERE kind='go_live_armed' ORDER BY id"):
        p = _payload(raw)
        d = str(p.get("date") or _et_date(ts) or "")[:10]
        try:
            t = int(p.get("tier") or 0)
        except (TypeError, ValueError):
            continue
        if d and t >= 1:
            out[d] = t
    return out


def tier_started_ts(store, tier: int) -> Optional[str]:
    """When this tier was FIRST armed, or None if it never was. Informational —
    attribution is per day (armed_tier_by_date), not from this bound."""
    for ts, raw in _rows(store, "SELECT ts, payload FROM journal "
                                "WHERE kind='go_live_armed' ORDER BY id"):
        try:
            if int(_payload(raw).get("tier") or 0) == int(tier):
                return str(ts)
        except (TypeError, ValueError):
            continue
    return None


def persistent_drift_timestamps(store, since_ts: str = "") -> list[str]:
    """Cycle-end timestamps at which reconcile drift had survived a cycle boundary:
    for consecutive completed cycles (a, b, c), drift raised during b AND during
    c, with neither b nor c ending reconcile_ok — the self-audit's test, applied
    to the whole window."""
    ends = _rows(store, "SELECT id, ts, payload FROM journal WHERE kind='cycle_end' ORDER BY id")
    if len(ends) < 3:
        return []

    def drift_between(lo: int, hi: int) -> bool:
        return bool(_rows(store, "SELECT 1 FROM journal WHERE kind='reconcile_drift' "
                                 "AND id > ? AND id <= ? LIMIT 1", (lo, hi)))

    def ok(raw: Any) -> bool:
        return _payload(raw).get("reconcile_ok") is True

    out: list[str] = []
    for i in range(2, len(ends)):
        a_id, _, _ = ends[i - 2]
        b_id, _, b_raw = ends[i - 1]
        c_id, c_ts, c_raw = ends[i]
        if since_ts and str(c_ts) < since_ts:
            continue
        if ok(b_raw) or ok(c_raw):
            continue
        if drift_between(a_id, b_id) and drift_between(b_id, c_id):
            out.append(str(c_ts))
    return out


def breach_events(store, since_ts: str = "") -> list[tuple[str, str]]:
    """[(ts, kind)] of every guardrail breach at/after since_ts, sorted."""
    out: list[tuple[str, str]] = []
    for ts, kind, raw in _rows(store, "SELECT ts, kind, payload FROM journal "
                                      "WHERE kind IN ('kill_switch','underlying_frozen',"
                                      "'cycle_fatal') AND ts >= ? ORDER BY id",
                               (since_ts or "",)):
        if _is_breach(str(kind), _payload(raw)):
            out.append((str(ts), str(kind)))
    for ts in persistent_drift_timestamps(store, since_ts):
        out.append((ts, "reconcile_drift_persistent"))
    out.sort()
    return out


# ─────────────────────────────── live days ───────────────────────────────

@dataclass
class LiveDay:
    date: str
    tier: Optional[int]          # the tier ARMED that day; None = unknown
    completed: bool = True       # every live cycle that day reached cycle_end
    kill_engaged: bool = False   # kill switch was on when a live cycle started
    frozen: bool = False         # an underlying was frozen when a live cycle started
    replayed: bool = False       # a cycle's asof disagreed with its own timestamp

    @property
    def clean(self) -> bool:
        return (self.tier is not None and self.completed and not self.kill_engaged
                and not self.frozen and not self.replayed)


def live_days(store) -> dict[str, LiveDay]:
    """Every date on which a LIVE cycle started, folded CONSERVATIVELY: any
    incomplete, kill-engaged, frozen, or replayed cycle on a date poisons the
    whole date. One ordered pass, tracking kill and freeze state as the journal
    recorded it."""
    armed = armed_tier_by_date(store)
    rows = _rows(store, "SELECT id, ts, kind, payload FROM journal WHERE kind IN "
                        "('cycle_start','cycle_end','kill_switch','underlying_frozen',"
                        "'frozen','underlying_unfrozen','unfreeze') ORDER BY id")
    kill_on = False
    frozen: set[str] = set()
    generic_frozen = False
    days: dict[str, LiveDay] = {}
    open_live: Optional[LiveDay] = None      # a live start awaiting its cycle_end

    def fold(day: LiveDay) -> LiveDay:
        cur = days.get(day.date)
        if cur is None:
            days[day.date] = day
            return day
        cur.completed = cur.completed and day.completed
        cur.kill_engaged = cur.kill_engaged or day.kill_engaged
        cur.frozen = cur.frozen or day.frozen
        cur.replayed = cur.replayed or day.replayed
        return cur

    for _id, ts, kind, raw in rows:
        p = _payload(raw)
        if kind == "kill_switch":
            kill_on = bool(p.get("engaged", True))
        elif kind in ("underlying_frozen", "frozen"):
            u = p.get("underlying") or p.get("symbol")
            if u:
                frozen.add(str(u).upper())
            else:
                generic_frozen = True
        elif kind in ("underlying_unfrozen", "unfreeze"):
            u = p.get("underlying") or p.get("symbol")
            if u:
                frozen.discard(str(u).upper())
            else:
                frozen.clear()
                generic_frozen = False
        elif kind == "cycle_start":
            if open_live is not None:            # previous live start never ended
                open_live.completed = False
                fold(open_live)
                open_live = None
            if str(p.get("broker") or "").lower() != LIVE_BROKER_NAME:
                continue
            d = _et_date(ts)
            if not d:
                continue
            asof = str(p.get("asof") or "")[:10]
            day = LiveDay(date=d, tier=armed.get(d), completed=True,
                          kill_engaged=kill_on,
                          frozen=bool(frozen) or generic_frozen,
                          replayed=bool(asof) and asof != d)
            open_live = fold(day)
        elif kind == "cycle_end":
            open_live = None                     # completed as recorded
    if open_live is not None:                    # the last start never ended
        open_live.completed = False
        fold(open_live)

    # The journal is the record of the past; for TODAY also ask the live switch.
    try:
        if getattr(store, "kill_switch", False):
            today = _et_date(datetime.now(timezone.utc).isoformat())
            if today in days:
                days[today].kill_engaged = True
    except Exception:
        pass
    return days


def live_session_dates(store, since_ts: str = "", tier: Optional[int] = None) -> list[str]:
    """Distinct dates a LIVE cycle started, at/after since_ts, attributed to
    `tier` if given. Includes non-clean days; `clean_days` is the subset."""
    since_d = _et_date(since_ts) if since_ts else ""
    return sorted(d for d, ld in live_days(store).items()
                  if (not since_d or d >= since_d)
                  and (tier is None or ld.tier == int(tier)))


def clean_days(store, since_ts: str, tier: Optional[int] = None) -> tuple[int, Optional[dict]]:
    """(count, last_breach): CLEAN live days attributed to `tier`, strictly after
    the later of since_ts and the most recent breach. A session on the breach's
    own date is not clean either."""
    breaches = breach_events(store, since_ts)
    last: Optional[dict] = None
    # The tier's first day COUNTS: arming happens in the morning, before that
    # day's cycle. A breach's own date does NOT: the session that breached is
    # not a clean one. Hence >= for the start and > for the breach.
    since_d = _et_date(since_ts) or ""
    breach_d = ""
    if breaches:
        ts, kind = breaches[-1]
        last = {"ts": ts, "kind": kind}
        breach_d = _et_date(ts) or ""
    n = sum(1 for d, ld in live_days(store).items()
            if d >= since_d and d > breach_d and ld.clean
            and (tier is None or ld.tier == int(tier)))
    return n, last


# ─────────────────────────────── live metrics ───────────────────────────────

def live_closed_positions(store, tier: Optional[int] = None) -> list[dict]:
    """Closed positions filled LIVE, attributed to the tier armed on the day
    they were OPENED (the day the risk was taken)."""
    rows = [dict(r) for r in _rows(store, "SELECT * FROM positions WHERE status='closed' "
                                          "AND fill_mode='live' ORDER BY id")]
    if tier is None:
        return rows
    armed = armed_tier_by_date(store)
    return [p for p in rows
            if armed.get(str(p.get("opened_asof") or "")[:10]) == int(tier)]


def live_metrics(store, costs=None, tier: Optional[int] = None) -> dict:
    """performance.compute_metrics over LIVE fills only (paper never enters),
    with the caller's cost model — ramp_status passes the CONFIGURED one."""
    from .performance import compute_metrics
    closed = live_closed_positions(store, tier)
    days = sorted({str(p.get("closed_asof")) for p in closed if p.get("closed_asof")})
    sessions = len(live_session_dates(store, tier=tier))
    return compute_metrics(closed, days, costs, sessions=sessions)


# ─────────────────────────────── the status ───────────────────────────────

def _unavailable(base: dict, why: str) -> dict:
    base["error"] = why
    base["advancement"] = {"ready_to_advance": False,
                           "reasons": [f"evidence unavailable: {why}"],
                           "note": "unreadable"}
    return base


def ramp_status(store, config, tier: Optional[int] = None) -> dict:
    """Everything the human needs to decide a tier edit — and everything gate #7
    needs to refuse an unearned one. Reports; never writes. `tier` defaults to
    the ACTIVE tier; gate #7 asks about `active - 1`, the tier whose evidence
    must justify the promotion. Any failure to READ the evidence is reported as
    exactly that, with ready_to_advance False."""
    from .golive import (_golive_cfg, _ramp_rows, _safe_int,
                         ramp_advancement_status)
    gl = _golive_cfg(config)
    active = _safe_int(gl.get("ramp_tier"), 0) or 0
    t = active if tier is None else int(tier)
    base: dict[str, Any] = {
        "tier": t, "active_tier": active, "tier_cfg": None, "started": None,
        "live_sessions": 0, "clean_days": 0, "last_breach": None,
        "live_metrics": None, "error": None,
        "advancement": {"ready_to_advance": False, "reasons": [], "note": ""}}
    if t < 1:
        base["advancement"]["reasons"] = ["tier 0 is paper — the ramp has not started"]
        base["advancement"]["note"] = "paper"
        return base
    tier_cfg = next((dict(r) for r in _ramp_rows(gl) if _safe_int(r.get("tier")) == t), None)
    base["tier_cfg"] = tier_cfg
    if tier_cfg is None:
        base["advancement"]["reasons"] = [f"tier {t} is not defined in go_live.ramp"]
        return base
    try:
        from .costs import CostModel
        try:
            costs = CostModel.from_config(config)
        except Exception as e:                    # defaults would be OPTIMISTIC
            return _unavailable(base, f"cost model unreadable: {e}")
        started = tier_started_ts(store, t)
        base["started"] = started
        if started is None:
            base["advancement"]["reasons"] = [
                f"tier {t} has never been armed (no go_live_armed event)"]
            return base
        cd, last = clean_days(store, started, tier=t)
        lm = live_metrics(store, costs, tier=t)
        base.update({"live_sessions": len(live_session_dates(store, started, tier=t)),
                     "clean_days": cd, "last_breach": last, "live_metrics": lm})
        base["advancement"] = ramp_advancement_status(tier_cfg, lm, cd)
        return base
    except EvidenceUnavailable as e:
        return _unavailable(base, str(e))
    except Exception as e:                        # noqa: BLE001 — never a confident number
        return _unavailable(base, f"{type(e).__name__}: {e}")
