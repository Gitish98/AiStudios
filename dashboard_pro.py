"""
dashboard_pro.py — a richer, fully self-contained, dark, mobile-first HTML
dashboard built ONLY from the local SQLite store (core.store.Store).

READ-ONLY. No CDN, no external fetch, no chart library, no risk-core imports.
Everything (CSS + JS + data) is inlined into a single HTML file written to
dashboard/out/dashboard_pro.html. The equity curve is drawn as an inline SVG
sparkline computed from the cumulative realized P&L of closed positions.

Usage: python dashboard_pro.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.config import REPO_ROOT
from core.options_math import MIN_IV_OBSERVATIONS
from core.store import Store

OUT_DIR = REPO_ROOT / "dashboard" / "out"


def _to_dt(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _dte(opened: Any, expiration: Any) -> Optional[int]:
    """Days-to-expiration relative to the open date (calendar days)."""
    o = _to_dt(opened)
    e = _to_dt(expiration)
    if o is None or e is None:
        return None
    return (e.date() - o.date()).days


def _num(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _equity_curve(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cumulative realized P&L over closed positions, in close order."""
    pts: list[dict[str, Any]] = []
    cum = 0.0
    for p in closed:
        cum += _num(p.get("realized_pnl"))
        pts.append({
            "ts": p.get("closed_ts") or p.get("closed_asof") or "",
            "cum": round(cum, 2),
        })
    return pts


def _closed_summary(closed: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(closed)
    wins = sum(1 for p in closed if _num(p.get("realized_pnl")) > 0)
    net = round(sum(_num(p.get("realized_pnl")) for p in closed), 2)
    win_rate = round(100.0 * wins / count, 1) if count else 0.0
    by_reason: dict[str, int] = {}
    for p in closed:
        r = p.get("exit_reason") or "unknown"
        by_reason[r] = by_reason.get(r, 0) + 1
    return {
        "count": count,
        "wins": wins,
        "losses": count - wins,
        "win_rate": win_rate,
        "net_pnl": net,
        "by_reason": by_reason,
    }


def _open_view(open_positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in open_positions:
        credit_ps = _num(p.get("entry_credit_ps"))
        is_credit = bool(p.get("is_credit"))
        contracts = int(p.get("contracts") or 0)
        out.append({
            "underlying": p.get("underlying") or "",
            "status": p.get("status") or "open",
            "structure": p.get("structure") or "",
            "is_credit": is_credit,
            "short_strike": p.get("short_strike"),
            "long_strike": p.get("long_strike"),
            "width": p.get("width"),
            "contracts": contracts,
            "expiration": (p.get("expiration") or "")[:10],
            "dte": _dte(p.get("opened_ts") or p.get("opened_asof"), p.get("expiration")),
            "credit_ps": round(credit_ps, 2),
            "max_loss": round(_num(p.get("max_loss")), 2),
            "mark_ps": (round(_num(p.get("last_mark_ps")), 4)
                        if p.get("last_mark_ps") is not None else None),
            "unrealized": (round(_num(p.get("unrealized_pnl")), 2)
                           if p.get("unrealized_pnl") is not None else None),
            "mark_ts": (str(p.get("last_mark_ts") or "")[:16].replace("T", " ")),
            "entry_fill_ps": (round(_num(p.get("entry_fill_ps")), 4)
                              if p.get("entry_fill_ps") is not None else None),
            "entry_slip_ps": (round(_num(p.get("entry_slip_ps")), 4)
                              if p.get("entry_slip_ps") is not None else None),
            # Exit proximity: how far to profit target / stop / the DTE close-out.
            # Credit spreads take profit as value DECAYS toward 0; debit spreads as
            # value GROWS. Reported as a 0-1 fraction so the UI can draw progress.
            "target_pct": _target_progress(p),
        })
    return out


def _target_progress(p: dict[str, Any]) -> Optional[float]:
    """Fraction of the way to this structure's profit target, 0..1, or None.

    Credit: entry credit decays toward 0 -> progress = (entry - mark) / (entry*0.5)
    Debit:  entry debit grows          -> progress = (mark - entry) / (entry*1.0)
    Uses the same default targets as core.positions.ManageParams."""
    mark = p.get("last_mark_ps")
    entry = _num(p.get("entry_credit_ps"))
    if mark is None or entry <= 0:
        return None
    mark = _num(mark)
    if bool(p.get("is_credit")):
        goal = entry * 0.50                      # profit_target_pct
        return round(max(0.0, min(1.0, (entry - mark) / goal)), 3) if goal > 0 else None
    goal = entry * 1.00                          # debit_profit_gain
    return round(max(0.0, min(1.0, (mark - entry) / goal)), 3) if goal > 0 else None


def _iv_progress(store: Store, target: int = MIN_IV_OBSERVATIONS) -> dict[str, Any]:
    """Store-only IV-rank bootstrap progress: distinct session-days accrued and the
    latest per-symbol ATM IV. This is what makes the empty state honest."""
    days, latest = 0, []
    try:
        days = int(store.conn.execute(
            "SELECT COUNT(DISTINCT asof) FROM iv_snapshots").fetchone()[0] or 0)
        rows = store.conn.execute(
            "SELECT symbol, atm_iv FROM iv_snapshots "
            "WHERE asof = (SELECT MAX(asof) FROM iv_snapshots) ORDER BY symbol"
        ).fetchall()
        latest = [{"symbol": r[0], "iv": round(float(r[1]), 3)} for r in rows]
    except Exception:
        pass
    return {"days": days, "target": target, "latest": latest}


def _graduation(closed: list[dict[str, Any]], min_days: int = 60,
                min_trades: int = 40, store=None) -> dict[str, Any]:
    """The graduation hurdle — delegated to the ONE real implementation.

    This used to compute expectancy from gross realized P&L with NO cost model,
    so it reported a positive expectancy (and a green 'eligible') in exactly the
    case the honest gate reports negative — a 57%-win-rate book that loses money
    after commissions and slippage. The dashboard is the surface actually looked
    at daily, so that made the measurement layer argue for going live precisely
    when the math said don't. Never fork the gate: call core.performance."""
    from core.performance import compute_metrics, graduation_status
    from core.costs import CostModel
    try:
        from core.config import load_config
        costs = CostModel.from_config(load_config())
    except Exception:
        costs = CostModel()

    days_list = sorted({p.get("closed_asof") for p in closed if p.get("closed_asof")})
    from core.performance import paper_record_sessions
    m = compute_metrics(closed, days_list, costs,
                        sessions=(paper_record_sessions(store) if store else None))
    grad = graduation_status(m, min_days=min_days, min_trades=min_trades)
    return {
        "trades": m["trades"], "min_trades": min_trades,
        "days": m["days"], "min_days": min_days,
        "closing_days": m.get("closing_days", 0),
        "net": m["net_pnl"], "gross": m["gross_pnl"], "costs": m["total_costs"],
        "expectancy": m["expectancy_net"] if m["trades"] else None,
        "cost_measured_pct": m.get("cost_measured_pct", 0.0),
        "cost_per_trade": m.get("cost_per_trade", 0.0),
        "eligible": grad["graduated"], "reasons": grad["reasons"],
    }


def _decisions(store: Store) -> list[dict[str, Any]]:
    """Per-symbol reason the last cycle did or did not trade. Five separate bugs
    have produced an identical healthy-looking "0 signals", so the dashboard shows
    the REASON, not just the count."""
    try:
        raw = store.get_kv("cycle_decisions")
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _slippage(store: Store) -> dict[str, Any]:
    """Realized execution quality across every position that has a captured fill.
    This is the number that decides whether a few points of edge survive contact
    with the market, so it belongs on the front page, not in a log."""
    from core.fills import summarize_slippage
    rows = [dict(r) for r in store.conn.execute(
        "SELECT entry_slip_ps, exit_slip_ps, fill_mode FROM positions "
        "WHERE entry_slip_ps IS NOT NULL OR exit_slip_ps IS NOT NULL").fetchall()]
    out = summarize_slippage(rows)
    out["paper_fills"] = sum(1 for r in rows if (r.get("fill_mode") or "paper") == "paper")
    out["live_fills"] = sum(1 for r in rows if r.get("fill_mode") == "live")
    return out


def _iv_series(store: Store) -> dict[str, list]:
    """Full per-symbol ATM-IV history — the data the system has been collecting
    daily since day 1. Small (sessions x 7 symbols) and the whole point of the
    bootstrap, so it belongs ON the page, not only in SQLite."""
    out: dict[str, list] = {}
    try:
        for r in store.conn.execute(
                "SELECT symbol, asof, atm_iv FROM iv_snapshots ORDER BY asof"):
            out.setdefault(r["symbol"], []).append([r["asof"], round(float(r["atm_iv"]), 4)])
    except Exception:
        pass
    return out


def _equity_series(store: Store) -> list[list]:
    """Daily account equity, one point per day, from each cycle's opening
    snapshot (journal cycle_start carries equity). This is the real
    'progress to date' curve — the closed-trade curve alone has one point."""
    out: dict[str, float] = {}
    try:
        for r in store.conn.execute(
                "SELECT ts, payload FROM journal WHERE kind='cycle_start' ORDER BY id"):
            try:
                eq = json.loads(r["payload"]).get("equity")
                if eq:
                    out[str(r["ts"])[:10]] = round(float(eq), 2)  # last per day wins
            except Exception:
                continue
    except Exception:
        pass
    return [[d, v] for d, v in sorted(out.items())]


def _cycle_history(store: Store, limit: int = 12) -> list[dict[str, Any]]:
    rows = []
    try:
        for r in store.conn.execute(
                "SELECT ts, payload FROM journal WHERE kind='cycle_end' "
                "ORDER BY id DESC LIMIT ?", (limit,)):
            try:
                p = json.loads(r["payload"])
                rows.append({"ts": r["ts"], "placed": p.get("placed", 0),
                             "rejected": p.get("rejected", 0),
                             "ok": bool(p.get("reconcile_ok")),
                             "secs": p.get("duration_secs")})
            except Exception:
                continue
    except Exception:
        pass
    return rows


def _trade_history(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in closed:
        out.append({
            "opened": (t.get("opened_asof") or "")[:10],
            "closed": (t.get("closed_asof") or "")[:10],
            "underlying": t.get("underlying"), "structure": t.get("structure"),
            "contracts": t.get("contracts"),
            "entry_ps": t.get("entry_credit_ps"), "exit_ps": t.get("exit_value_ps"),
            "reason": t.get("exit_reason"), "pnl": t.get("realized_pnl"),
            "entry_slip": t.get("entry_slip_ps"), "exit_slip": t.get("exit_slip_ps"),
        })
    return out


def _gather(store: Store) -> dict[str, Any]:
    closed = store.get_closed_positions()
    # ACTIVE = open + pending. A pending position (working/partially-filled
    # order) is real exposure at the broker — five live spreads rendering as
    # "no open positions" misrepresents the book exactly when the operator most
    # wants to watch it fill.
    open_positions = store.get_active_positions()

    # Account + last-cycle come from read-only store markers the cycle writes.
    equity = store.get_kv("equity")
    cash = store.get_kv("cash")
    buying_power = store.get_kv("buying_power")
    last_cycle = store.get_kv("last_cycle")
    try:
        last_cycle = json.loads(last_cycle) if last_cycle else None
    except Exception:
        last_cycle = None
    try:
        from core.config import load_config
        watchlist = load_config().watchlist
    except Exception:
        watchlist = []

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": (store.get_kv("mode") or "paper").upper(),
        "broker": store.get_kv("broker") or "sim",
        "kill_switch": store.kill_switch,
        "frozen": store.frozen_underlyings(),
        "account": {
            "equity": _num(equity) if equity is not None else None,
            "cash": _num(cash) if cash is not None else None,
            "buying_power": _num(buying_power) if buying_power is not None else None,
        },
        "last_cycle": last_cycle,
        "decisions": _decisions(store),
        "slippage": _slippage(store),
        "iv": _iv_progress(store),
        "graduation": _graduation(closed, store=store),
        "watchlist": watchlist,
        "equity_curve": _equity_curve(closed),
        "open_positions": _open_view(open_positions),
        "closed_summary": _closed_summary(closed),
        "journal": store.recent(limit=40),
        "iv_series": _iv_series(store),
        "equity_series": _equity_series(store),
        "cycles": _cycle_history(store),
        "trades": _trade_history(closed),
        "fx_rates": store.get_fx_rates(),
    }


def build_pro(store_path: Optional[Path] = None,
              out_dir: Optional[Path] = None) -> Path:
    """Render the dashboard. `out_dir` exists so TESTS never overwrite the served
    file: build_pro used to write to the production path regardless of which store
    it read, so running the suite on the VM replaced the live dashboard with test
    fixtures until the next cycle regenerated it."""
    store = Store(Path(store_path) if store_path else None)
    try:
        data = _gather(store)
    finally:
        store.close()

    out_root = Path(out_dir) if out_dir else OUT_DIR
    out_root.mkdir(parents=True, exist_ok=True)
    out = out_root / "dashboard_pro.html"
    out.write_text(_render(data), encoding="utf-8")
    # Raw-data export: everything the system has collected, one downloadable
    # file next to the page (linked from the History section).
    try:
        (out_root / "history.json").write_text(json.dumps({
            "generated": data.get("generated"),
            "equity_by_day": data.get("equity_series"),
            "atm_iv_by_symbol": data.get("iv_series"),
            "closed_trades": data.get("trades"),
            "recent_cycles": data.get("cycles"),
            "usdcad_by_day": data.get("fx_rates"),
        }, indent=1, default=str), encoding="utf-8")
    except Exception:
        pass
    return out


# ── SVG sparkline (server-side, no JS, no chart lib) ─────────────────────────
def _sparkline_svg(curve: list[dict[str, Any]]) -> str:
    W, H, PAD = 320, 90, 6
    if not curve:
        return (f'<svg class="spark" viewBox="0 0 {W} {H}" width="100%" '
                f'preserveAspectRatio="none"><text x="{W/2}" y="{H/2}" '
                f'fill="#7a7a98" font-size="12" text-anchor="middle">no closed trades yet'
                f'</text></svg>')

    vals = [float(p["cum"]) for p in curve]
    # Anchor the curve to a zero baseline so a single point still renders.
    series = [0.0] + vals if len(vals) == 1 else vals
    lo, hi = min(series), max(series)
    if hi == lo:
        hi = lo + 1.0
    n = len(series)

    def x(i: int) -> float:
        if n == 1:
            return W / 2
        return PAD + (W - 2 * PAD) * i / (n - 1)

    def y(v: float) -> float:
        return PAD + (H - 2 * PAD) * (1 - (v - lo) / (hi - lo))

    pts = [(x(i), y(v)) for i, v in enumerate(series)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = (f"{PAD:.1f},{H - PAD:.1f} " + line +
            f" {W - PAD:.1f},{H - PAD:.1f}")
    last = vals[-1]
    stroke = "#60cc88" if last >= 0 else "#ff6b6b"
    zero_y = y(0.0) if lo <= 0 <= hi else None
    zero = (f'<line x1="{PAD}" y1="{zero_y:.1f}" x2="{W - PAD}" y2="{zero_y:.1f}" '
            f'stroke="rgba(255,255,255,.14)" stroke-width="1" stroke-dasharray="3,3"/>'
            if zero_y is not None else "")
    return (
        f'<svg class="spark" viewBox="0 0 {W} {H}" width="100%" '
        f'preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="{stroke}" fill-opacity="0.12"/>'
        f'{zero}'
        f'<polyline points="{line}" fill="none" stroke="{stroke}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


def _render(data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    # Equity change since day 1 — the real progress curve (one point per session
    # from cycle snapshots). Falls back to the closed-trade curve when empty.
    eq = data.get("equity_series") or []
    if len(eq) >= 2:
        base = float(eq[0][1])
        spark_curve = [{"ts": d, "cum": round(float(v) - base, 2)} for d, v in eq]
    else:
        spark_curve = data["equity_curve"]
    spark = _sparkline_svg(spark_curve)
    wr = data["closed_summary"]["win_rate"]
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>AiStudios — Pro Dashboard</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
background:#0f0f1a;color:#e8e8f4;padding:16px;padding-bottom:40px;-webkit-text-size-adjust:100%}}
.banner{{border-radius:12px;padding:14px 16px;margin-bottom:14px;font-weight:700;letter-spacing:.05em}}
.paper{{background:rgba(80,200,120,.15);color:#60cc88;border:1px solid rgba(80,200,120,.3)}}
.live{{background:rgba(255,80,80,.15);color:#ff6b6b;border:1px solid rgba(255,80,80,.4)}}
.kill{{background:rgba(255,180,40,.14);color:#ffb648;border:1px solid rgba(255,180,40,.35);
border-radius:10px;padding:10px 14px;margin-bottom:14px;font-weight:600;font-size:13px}}
.killoff{{background:rgba(80,200,120,.1);color:#60cc88;border:1px solid rgba(80,200,120,.25);
border-radius:10px;padding:10px 14px;margin-bottom:14px;font-weight:600;font-size:13px}}
h1{{font-size:18px;margin-bottom:2px}}
.sub{{color:#8888aa;font-size:12px;margin-bottom:16px}}
.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:18px}}
.card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
border-radius:12px;padding:14px}}
.card .k{{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#8888aa}}
.card .v{{font-size:20px;font-weight:700;margin-top:4px}}
.pos{{color:#60cc88}}.neg{{color:#ff6b6b}}
.sec-title{{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:#8888aa;margin:18px 0 10px}}
.curve{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
border-radius:12px;padding:12px;margin-bottom:6px}}
.spark{{display:block}}
.row{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:10px;
padding:11px 13px;margin-bottom:8px;font-size:13px}}
.row .top{{display:flex;justify-content:space-between;gap:8px;margin-bottom:3px}}
.tag{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
padding:2px 7px;border-radius:20px}}
.ok{{background:rgba(80,200,120,.16);color:#60cc88}}
.deb{{background:rgba(108,108,255,.16);color:#9a9aff}}
.muted{{color:#7a7a98}}
.ts{{color:#6a6a88;font-size:11px}}
.empty{{color:#7a7a98;font-size:13px;padding:14px 0}}
.reasons{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}}
.chip{{font-size:11px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
border-radius:20px;padding:3px 9px;color:#b8b8d8}}
.info{{background:rgba(90,140,240,.14);color:#9fb9ff;border:1px solid rgba(90,140,240,.3);
border-radius:12px;padding:12px 14px;margin-bottom:14px;font-size:13px;line-height:1.5}}
.bar{{height:8px;background:rgba(255,255,255,.08);border-radius:99px;overflow:hidden;margin:8px 0}}
.barfill{{height:100%;background:#9fb9ff;border-radius:99px}}
.gate{{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;color:#b8b8d8}}
.gate b{{color:#e8e8f4;font-weight:600}}
.poscard{{background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.09);
border-radius:12px;padding:13px 15px;margin-bottom:10px}}
.poscard .hd{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px}}
.poscard .sym{{font-size:15px;font-weight:700;letter-spacing:.02em}}
.hdr{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.pill{{font-size:11px;font-weight:700;letter-spacing:.08em;padding:3px 10px;border-radius:20px}}
.pill.paper{{background:rgba(80,200,120,.15);color:#60cc88;border:1px solid rgba(80,200,120,.3)}}
.pill.live{{background:rgba(255,80,80,.15);color:#ff6b6b;border:1px solid rgba(255,80,80,.4)}}
.status{{font-size:12.5px;color:#8888aa;margin:6px 0 16px;line-height:1.6}}
.okdot{{color:#60cc88}}.baddot{{color:#ffb648}}
.minis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
.mini{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:9px 11px}}
.mini .t{{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px}}
.mini .t b{{font-weight:700}}
.note{{font-size:11.5px;color:#7a7a98;margin:8px 0 2px;line-height:1.5}}
a{{color:#9fb9ff}}
.rngs{{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 8px}}
.rng{{font-size:11px;font-weight:700;letter-spacing:.04em;color:#8888aa;background:rgba(255,255,255,.05);
border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:4px 10px;cursor:pointer;user-select:none}}
.rng.on{{color:#0f0f1a;background:#9fb9ff;border-color:#9fb9ff}}
.eqread{{font-size:13px;color:#c8c8e0;margin-bottom:6px;min-height:18px}}
.eqread b{{font-size:15px;color:#e8e8f4}}
#eqchart svg{{display:block;touch-action:none}}
.pnl{{font-size:19px;font-weight:700}}
.grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:10px 0 6px}}
.grid3 .k{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#8888aa}}
.grid3 .v{{font-size:13px;font-weight:600;margin-top:2px}}
.prog{{height:6px;background:rgba(255,255,255,.08);border-radius:99px;overflow:hidden;margin:3px 0 2px}}
.progf{{height:100%;border-radius:99px}}
.tbl{{width:100%;border-collapse:collapse;font-size:12.5px}}
.tbl th{{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.08em;
color:#8888aa;font-weight:600;padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.09)}}
.tbl td{{padding:6px;border-bottom:1px solid rgba(255,255,255,.05);color:#c8c8e0}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}}
</style></head><body>
<div id="app">
<div class="sec-title">Equity curve</div>
<div class="curve" id="curve">{spark}</div>
<div class="hidden-winrate" style="display:none" data-winrate="{wr}">win rate {wr}%</div>
</div>
<script>
var D = {payload};
function esc(s){{return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function money(v){{if(v==null)return '—';var n=Number(v);return isNaN(n)?'—':'$'+n.toLocaleString(undefined,{{maximumFractionDigits:0}});}}
function signed(v){{var n=Number(v||0);var s=(n>=0?'+':'-')+'$'+Math.abs(n).toLocaleString(undefined,{{maximumFractionDigits:0}});return '<span class="'+(n>=0?'pos':'neg')+'">'+s+'</span>';}}
var a = D.account||{{}};
var cs = D.closed_summary||{{}};
var curveHTML = document.getElementById('curve').outerHTML;

// ── helpers ──────────────────────────────────────────────────────────────
function et(ts){{ if(!ts) return '—';
  try {{ return new Date(String(ts)).toLocaleString('en-US',
      {{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}}) + ' ET'; }}
  catch(e) {{ return String(ts).slice(0,16).replace('T',' '); }} }}
function fmtv(v){{ if(v==null) return '—';
  if (typeof v === 'object') return esc(JSON.stringify(v)).slice(0,110);
  if (typeof v === 'number') return esc(Math.round(v*10000)/10000);
  return esc(String(v)); }}
function msvg(vals, color){{
  if (!vals || vals.length < 2) return '<div class="muted" style="font-size:11px">collecting…</div>';
  var W=140, H=34, P=2, lo=Math.min.apply(null,vals), hi=Math.max.apply(null,vals);
  if (hi===lo) hi=lo+1e-6;
  var pts = vals.map(function(v,i){{
    return (P+(W-2*P)*i/(vals.length-1)).toFixed(1)+','+(P+(H-2*P)*(1-(v-lo)/(hi-lo))).toFixed(1);
  }}).join(' ');
  return '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="34" preserveAspectRatio="none">'+
         '<polyline points="'+pts+'" fill="none" stroke="'+color+'" stroke-width="1.6" '+
         'stroke-linejoin="round" stroke-linecap="round"/></svg>'; }}

var h = '';

// ── header + status strip ────────────────────────────────────────────────
h += '<div class="hdr"><h1>AiStudios</h1><span class="pill '+(D.mode==='PAPER'?'paper':'live')+'">'+esc(D.mode)+'</span></div>';
var lc = D.last_cycle || {{}};
h += '<div class="status">'
  + (D.kill_switch ? '<span class="baddot">●</span> trading HALTED (kill switch)'
                   : '<span class="okdot">●</span> trading enabled')
  + ' · book '+(lc.reconcile_ok ? '<span class="okdot">matches broker</span>'
                                : '<span class="baddot">differs from broker</span>')
  + '<br>'+esc(String(D.broker||'sim'))+' · last cycle '+et(lc.ts)+' — '
  + (lc.signals||0)+' signal'+((lc.signals||0)==1?'':'s')+', '+(lc.placed||0)+' order'+((lc.placed||0)==1?'':'s')+' placed'
  + '</div>';

// ── alerts (only when they matter) ───────────────────────────────────────
if (D.kill_switch) h += '<div class="kill">⚠︎ KILL SWITCH ENGAGED — all new orders are blocked until cleared</div>';
var _fz = Object.keys(D.frozen||{{}});
if (_fz.length) h += '<div class="kill">❄︎ FROZEN (possible assignment): '+_fz.map(esc).join(', ')+' — no automated orders on these until cleared (cli.py unfreeze)</div>';
var iv = D.iv||{{}};
var boot = (D.decisions||[]).filter(function(d){{ return d.iv_rank==null; }})
                            .map(function(d){{ return d.symbol; }});
if (boot.length && (iv.days||0) < (iv.target||60)) {{
  h += '<div class="info">'+esc(boot.join(', '))+' still bootstrapping IV rank — day '+
       (iv.days||0)+' of '+(iv.target||60)+'. Those symbols stand aside; the rest use a '+
       'real 252-day benchmark rank and can trade today.</div>';
}}

// ── account ──────────────────────────────────────────────────────────────
var openRisk = 0; (D.open_positions||[]).forEach(function(p){{ openRisk += Number(p.max_loss||0); }});
h += '<div class="cards">';
h += '<div class="card"><div class="k">Account equity</div><div class="v">'+money(a.equity)+'</div></div>';
h += '<div class="card"><div class="k">Realized P&L (all time)</div><div class="v">'+signed(cs.net_pnl)+'</div></div>';
h += '<div class="card"><div class="k">Money at risk now</div><div class="v">'+money(openRisk)+'</div><div class="note" style="margin:2px 0 0">worst case across open positions</div></div>';
h += '<div class="card"><div class="k">Buying power</div><div class="v">'+money(a.buying_power)+'</div></div>';
h += '</div>';

// ── positions ────────────────────────────────────────────────────────────
h += '<div class="sec-title">Positions</div>';
var ops = D.open_positions||[];
if (!ops.length) h += '<div class="empty">No open positions — the system is watching, not trading.</div>';
ops.forEach(function(p){{
  var lbl = p.is_credit?'credit':'debit';
  var strikes = (p.short_strike!=null?p.short_strike:'—')+'/'+(p.long_strike!=null?p.long_strike:'—');
  var u = p.unrealized;
  var pcls = (u==null)?'muted':(u>=0?'pos':'neg');
  var pval = (u==null)?'—':((u>=0?'+':'-')+'$'+Math.abs(u).toLocaleString(undefined,{{maximumFractionDigits:0}}));
  h += '<div class="poscard">';
  var ptag = (p.status==='pending') ? ' <span class="tag" style="background:rgba(255,200,80,.16);color:#ffd280">FILLING</span>' : '';
  h += '<div class="hd"><span class="sym">'+esc(p.underlying||'—')+' '+esc(strikes)+
       ' <span class="tag '+(p.is_credit?'ok':'deb')+'">'+lbl+'</span>'+ptag+'</span>'+
       '<span class="pnl '+pcls+'">'+pval+'</span></div>';
  h += '<div class="grid3">'+
       '<div><div class="k">Entry</div><div class="v">'+esc(p.credit_ps)+' /sh'+
         (p.entry_fill_ps!=null?' <span class="muted">(fill '+esc(p.entry_fill_ps)+')</span>':'')+'</div></div>'+
       '<div><div class="k">Now worth</div><div class="v">'+(p.mark_ps==null?'<span class="muted">—</span>':esc(p.mark_ps)+' /sh')+'</div></div>'+
       '<div><div class="k">Max loss</div><div class="v">'+money(p.max_loss)+'</div></div>'+
       '</div>';
  var tp = p.target_pct;
  if (tp!=null){{
    h += '<div class="k" style="font-size:10px;color:#8888aa">Progress to profit target</div>'+
         '<div class="prog"><div class="progf" style="width:'+Math.round(tp*100)+'%;background:'+(tp>=1?'#60cc88':'#9fb9ff')+'"></div></div>'+
         '<div class="muted" style="font-size:11px">'+Math.round(tp*100)+'% there</div>';
  }}
  var dteTxt = (p.dte==null?'—':p.dte+' days to expiry');
  var dteCls = (p.dte!=null && p.dte<=21)?'neg':'muted';
  h += '<div class="muted" style="font-size:11.5px;margin-top:7px">'+
       esc(p.contracts)+' contract'+(p.contracts==1?'':'s')+' · <span class="'+dteCls+'">'+dteTxt+'</span> ('+esc(p.expiration||'—')+')'+
       (p.entry_slip_ps!=null?' · fill slippage '+(p.entry_slip_ps>=0?'+':'')+esc(p.entry_slip_ps)+'/sh':'')+
       '</div>';
  h += '</div>';
}});

// ── road to live ─────────────────────────────────────────────────────────
h += '<div class="sec-title">Road to live</div>';
var g = D.graduation||{{}};
var tpct = Math.min(100, Math.round(100*(g.trades||0)/(g.min_trades||40)));
var dpct = Math.min(100, Math.round(100*(g.days||0)/(g.min_days||60)));
h += '<div class="card" style="margin-bottom:10px"><div class="k" style="margin-bottom:6px">Graduation gate</div>';
h += '<div class="gate"><span>Closed trades</span><b>'+(g.trades||0)+' / '+(g.min_trades||40)+'</b></div>';
h += '<div class="bar"><div class="barfill" style="width:'+tpct+'%"></div></div>';
h += '<div class="gate"><span>Paper record</span><b>'+(g.days||0)+' / '+(g.min_days||60)+' sessions</b></div>';
h += '<div class="bar"><div class="barfill" style="width:'+dpct+'%"></div></div>';
h += '<div class="note" style="margin:-2px 0 8px">Trading sessions since the FIRST trade — the length of the track record. '
   + (g.closing_days!=null ? (g.closing_days+' of them had a close.') : '')
   + ' (Days the system merely ran before it started trading do not count.)</div>';
h += '<div class="gate"><span>Net expectancy (after costs)</span><b>'+(g.expectancy==null?'—':signed(g.expectancy)+' /trade')+'</b></div>';
function cents(v){{var n=Number(v||0);return (n<0?'-':'')+'$'+Math.abs(n).toFixed(2);}}
if (g.cost_per_trade)
  h += '<div class="note" style="margin:-2px 0 8px">Gross '+cents(g.gross)+' minus '+cents(g.costs)
     + ' of costs = <b style="color:'+((g.net||0)>=0?'#60cc88':'#ff6b6b')+'">'+cents(g.net)+' net</b>. '
     + 'That is '+cents(g.cost_per_trade)+' of cost per trade, '
     + Math.round((g.cost_measured_pct||0)*100)+'% of it from MEASURED fills (the rest is modeled '
     + '— the modeled part is the optimistic part).</div>';
h += '<div style="margin-top:6px;font-size:12px;color:'+(g.eligible?'#60cc88':'#ff9a6b')+'">'
   + (g.eligible?'Clears the minimum bar — a hurdle, not a recommendation to go live.'
               :'Not yet eligible. All THREE must pass independently, and the final call is always human.')+'</div>';
var _rs = g.reasons||[];
if (_rs.length) {{
  h += '<div class="reasons" style="margin-top:8px">';
  _rs.forEach(function(r){{ h += '<span class="chip" style="color:#ff9a6b">'+esc(r)+'</span>'; }});
  h += '</div>';
}}
h += '</div>';
var ivpct = Math.min(100, Math.round(100*(iv.days||0)/(iv.target||60)));
h += '<div class="card"><div class="k" style="margin-bottom:6px">IV-rank bootstrap (XLK/XLF/XLE)</div>';
h += '<div class="gate"><span>Sessions of IV collected</span><b>'+(iv.days||0)+' / '+(iv.target||60)+'</b></div>';
h += '<div class="bar"><div class="barfill" style="width:'+ivpct+'%"></div></div>';
h += '<div class="note">SPY/QQQ/IWM/DIA already trade on real 252-day Cboe vol history; the three sector funds unlock when their own history reaches '+(iv.target||60)+' sessions.</div></div>';

// ── today, per symbol ────────────────────────────────────────────────────
var dec = D.decisions||[];
if (dec.length){{
  h += '<div class="sec-title">Last cycle — what each symbol did</div><div class="card" style="padding:8px 10px">';
  h += '<table class="tbl"><tr><th>Symbol</th><th>IV rank</th><th>Basis</th><th>Decision</th></tr>';
  dec.forEach(function(d){{
    var r = d.iv_rank;
    var col = (r==null)?'#7a7a98':(r>=0.40?'#60cc88':'#8888aa');
    var rtxt = (r==null)?'—':Math.round(r*100)+'%';
    h += '<tr><td><b>'+esc(d.symbol)+'</b></td>'+
         '<td><span class="dot" style="background:'+col+'"></span>'+rtxt+'</td>'+
         '<td class="muted">'+esc((d.iv_source||'').replace('cboe:',''))+'</td>'+
         '<td class="muted">'+esc(d.note||'')+'</td></tr>';
  }});
  h += '</table><div class="note">IV rank ≥ 40% is what premium-selling needs; green dot = cleared. "Basis" is which volatility series the rank comes from.</div></div>';
}}

// ── history ──────────────────────────────────────────────────────────────
h += '<div class="sec-title">History</div>';
h += '<div class="card" style="margin-bottom:10px"><div class="k" style="margin-bottom:6px">Account equity</div>'
   + '<div class="rngs" id="eqrngs"></div>'
   + '<div class="rngs" id="eqccy" style="margin-top:-2px"></div>'
   + '<div class="eqread" id="eqread">—</div>'
   + '<div id="eqchart"></div>'
   + '<div class="note">Honest caveat: the USD view includes currency movement plus simulated '
   + 'paper interest — most of the USD rise to date is CAD strengthening, not trading. '
   + 'Switch to CAD (native) to see the account in its own currency, where FX noise '
   + 'disappears. Pure trading results are the "Realized P&L" tile above; the graduation '
   + 'gate uses only per-trade P&L net of costs, so FX can never make the system look '
   + 'tradeworthy.</div></div>';

var ivs = D.iv_series||{{}};
var syms = Object.keys(ivs).sort();
if (syms.length){{
  h += '<div class="card" style="margin-bottom:10px"><div class="k" style="margin-bottom:8px">Implied volatility collected daily ('+((ivs[syms[0]]||[]).length)+' sessions)</div><div class="minis">';
  syms.forEach(function(sym){{
    var series = (ivs[sym]||[]).map(function(x){{ return Number(x[1]); }});
    var last = series.length ? series[series.length-1] : null;
    h += '<div class="mini"><div class="t"><b>'+esc(sym)+'</b><span class="muted">'+(last==null?'—':(last*100).toFixed(1)+'%')+'</span></div>'+msvg(series, '#9fb9ff')+'</div>';
  }});
  h += '</div><div class="note">Each line is one symbol’s at-the-money implied volatility, one reading per session — the raw material for the IV rank above.</div></div>';
}}

var tr = D.trades||[];
h += '<div class="card" style="margin-bottom:10px"><div class="k" style="margin-bottom:6px">Every closed trade</div>';
if (!tr.length) h += '<div class="empty">None yet.</div>';
else {{
  h += '<table class="tbl"><tr><th>Closed</th><th>Trade</th><th>Why it exited</th><th>P&L</th></tr>';
  tr.forEach(function(t){{
    h += '<tr><td class="muted">'+esc(t.closed)+'</td>'+
         '<td><b>'+esc(t.underlying)+'</b> '+esc(String(t.structure||'').replace(/_/g,' '))+' ×'+esc(t.contracts)+'</td>'+
         '<td class="muted">'+esc(String(t.reason||'').replace(/_/g,' '))+'</td>'+
         '<td>'+signed(t.pnl)+'</td></tr>';
  }});
  h += '</table>';
}}
h += '</div>';

var cyc = D.cycles||[];
if (cyc.length){{
  h += '<div class="card" style="margin-bottom:10px"><div class="k" style="margin-bottom:6px">Recent cycles (the twice-daily runs)</div>';
  h += '<table class="tbl"><tr><th>When</th><th>Orders</th><th>Book check</th><th>Took</th></tr>';
  cyc.forEach(function(c){{
    h += '<tr><td class="muted">'+et(c.ts)+'</td>'+
         '<td>'+(c.placed||0)+' placed'+((c.rejected||0)?(', '+c.rejected+' rejected'):'')+'</td>'+
         '<td>'+(c.ok?'<span class="okdot">✓ matches broker</span>':'<span class="baddot">differs</span>')+'</td>'+
         '<td class="muted">'+(c.secs==null?'—':Math.round(c.secs)+'s')+'</td></tr>';
  }});
  h += '</table></div>';
}}
h += '<div class="note">Full raw history (every equity point, IV reading, trade and cycle): <a href="history.json" download>download history.json</a>. The complete record lives in the trading database on the server.</div>';

// ── execution quality ────────────────────────────────────────────────────
var sl = D.slippage||{{}};
if (sl.legs_measured){{
  h += '<div class="sec-title">Execution quality</div><div class="card">';
  h += '<div class="gate"><span>Mean entry slippage</span><b>'+
       (sl.mean_entry_slip_ps==null?'—':(sl.mean_entry_slip_ps>=0?'+':'')+sl.mean_entry_slip_ps+' /sh')+'</b></div>';
  if (sl.mean_exit_slip_ps!=null)
    h += '<div class="gate"><span>Mean exit slippage</span><b>'+(sl.mean_exit_slip_ps>=0?'+':'')+sl.mean_exit_slip_ps+' /sh</b></div>';
  h += '<div class="gate"><span>Worst</span><b>'+(sl.worst_slip_ps==null?'—':sl.worst_slip_ps+' /sh')+'</b></div>';
  h += '<div class="gate"><span>Coverage</span><b>'+Math.round((sl.coverage||0)*100)+'% of positions</b></div>';
  h += '<div class="note">Slippage = how much worse the real fill was than the intended price. Positive = worse. On a few-cents credit this is the difference between edge and none.</div>';
  if (sl.paper_fills && !sl.live_fills)
    h += '<div class="note">All measured fills are PAPER — IBKR paper fills optimistically, so treat these as a LOWER bound until live fills exist.</div>';
  h += '</div>';
}}

// ── closed-trade stats (kept compact) ────────────────────────────────────
h += '<div class="sec-title">Closed trades</div>';
h += '<div class="cards">';
h += '<div class="card"><div class="k">Trades</div><div class="v">'+esc(cs.count||0)+'</div></div>';
h += '<div class="card"><div class="k">Win rate</div><div class="v">'+esc(cs.win_rate||0)+'%</div></div>';
h += '</div>';

// ── activity, in plain language ──────────────────────────────────────────
var KINDS = {{
  cycle_end:'Cycle finished', cycle_start:'Cycle started', order_placed:'Order sent to broker',
  position_closed:'Position closed', risk_decision:'Risk gate decision',
  reconcile_drift:'Book vs broker mismatch', close_escalated:'Exit re-priced (conceding to fill)',
  close_pending:'Exit order working at broker', close_expired:'Exit order expired unfilled',
  entry_filled:'Entry filled', entry_dropped:'Unfilled order cleaned up',
  partial_fill_adopted:'Partial fill adopted (resized to reality)',
  partial_fill_detected:'PARTIAL FILL — needs a look', kill_switch:'KILL SWITCH',
  underlying_frozen:'Symbol FROZEN for review', underlying_unfrozen:'Symbol unfrozen',
  close_ambiguous_assignment:'Possible assignment — held for review',
  broker_book_empty:'Broker reported an EMPTY book', offhours_skip:'Run attempted outside market hours',
  exdiv_risk_exit:'Closed early to dodge dividend assignment', degraded_sim_cycle:'Refused to run without the broker',
  expiry_unverified:'Expiry not booked (broker unverified)', close_blocked_legs_diverged:'Exit BLOCKED — legs differ at broker',
  cycle_fatal:'CYCLE FAILED', manage_error:'Error managing a position', close_failed:'Broker rejected an exit'
}};
function describe(j){{
  var p = j.payload||{{}};
  var k = j.kind;
  if (k==='cycle_end') return (p.placed||0)+' placed, '+(p.rejected||0)+' rejected · book '+(p.reconcile_ok?'matches broker':'DIFFERS')+(p.duration_secs?(' · '+Math.round(p.duration_secs)+'s'):'');
  if (k==='reconcile_drift'){{
    var ds = p.drift||[];
    return ds.slice(0,3).map(function(d){{ return (d.kind||'')+': '+(d.leg||d.underlying||''); }}).join(' · ') + (ds.length>3?(' · +'+(ds.length-3)+' more'):'');
  }}
  if (k==='order_placed') return esc(String(p.rationale||'').slice(0,140)) || ('order '+fmtv(p.client_order_id));
  if (k==='position_closed') return (p.reason?String(p.reason).replace(/_/g,' ')+' · ':'')+'P&L '+fmtv(p.realized_pnl);
  if (k==='risk_decision') return (p.approved?'APPROVED':'REJECTED')+' — '+fmtv(p.underlying)+' '+String(p.strategy||'').replace(/_/g,' ');
  if (k==='close_escalated') return 'attempt '+fmtv(p.attempt)+': limit '+fmtv(p.limit)+' (fair value '+fmtv(p.mark)+')';
  var keys = Object.keys(p).slice(0,3);
  return keys.map(function(kk){{ return kk.replace(/_/g,' ')+': '+fmtv(p[kk]); }}).join(' · ');
}}
h += '<div class="sec-title">Recent activity</div>';
var jr = (D.journal||[]).filter(function(j){{
  return j.kind!=='iv_rank_source' && j.kind!=='capability_warning' && j.kind!=='cycle_start';
}}).slice(0,12);
if (!jr.length) h += '<div class="empty">Nothing yet.</div>';
jr.forEach(function(j){{
  h += '<div class="row"><div class="top"><b>'+esc(KINDS[j.kind]||String(j.kind).replace(/_/g,' '))+'</b><span class="ts">'+et(j.ts)+'</span></div>'+
       '<div class="muted">'+describe(j)+'</div></div>';
}});

document.getElementById('app').innerHTML = h;

// ── interactive equity chart: range buttons + hover/touch crosshair ──────
(function(){{
  var USD = (D.equity_series||[]).map(function(r){{ return [String(r[0]), Number(r[1])]; }});
  var FX = D.fx_rates||{{}};
  // CAD view = USD equity x that day's REAL recorded rate. Days with no recorded
  // rate are OMITTED, never interpolated — an honest gap beats a smooth fiction.
  var CAD = USD.filter(function(r){{ return FX[r[0]] > 0; }})
               .map(function(r){{ return [r[0], r[1]*FX[r[0]]]; }});
  var CCY = 'USD';
  var ALL = USD;
  var box = document.getElementById('eqchart');
  var rngs = document.getElementById('eqrngs');
  var read = document.getElementById('eqread');
  if (!box) return;
  if (ALL.length < 2) {{
    box.innerHTML = '<div class="muted" style="font-size:12px;padding:12px 0">Collecting — one equity point is banked per session; the curve appears from day 2.</div>';
    if (rngs) rngs.style.display = 'none';
    return;
  }}
  var RANGES = [['1D',1],['5D',7],['1M',31],['3M',92],['6M',183],['1Y',366],['All',null]];
  var cur = 'All';
  var G = null;   // geometry of the current draw, for the crosshair

  function fmtD(iso){{
    try {{ return new Date(iso+'T12:00:00Z').toLocaleDateString('en-US',
        {{month:'short',day:'numeric',year:'numeric'}}); }} catch(e) {{ return iso; }}
  }}
  function fmt$(v){{ return (CCY==='CAD'?'C$':'$')+Math.round(v).toLocaleString(); }}

  function setRead(i){{
    if (!G) return;
    var d = G.pts[i], chg = d[1]-G.base, pc = G.base ? (100*chg/G.base) : 0;
    read.innerHTML = esc(fmtD(d[0]))+' · <b>'+fmt$(d[1])+'</b> · '
      + '<span class="'+(chg>=0?'pos':'neg')+'">'+(chg>=0?'+':'-')+fmt$(Math.abs(chg))
      + ' ('+(chg>=0?'+':'')+pc.toFixed(2)+'%)</span>'
      + ' <span class="muted">vs start of range</span>';
  }}

  function draw(){{
    ALL = (CCY==='CAD') ? CAD : USD;
    if (ALL.length < 2) {{
      box.innerHTML = '<div class="muted" style="font-size:12px;padding:12px 0">Not enough '+CCY+' history yet.</div>';
      read.textContent = '—';
      return;
    }}
    var days = null;
    RANGES.forEach(function(r){{ if (r[0]===cur) days = r[1]; }});
    var pts = ALL;
    if (days != null) {{
      var last = new Date(ALL[ALL.length-1][0]+'T12:00:00Z');
      var cut = new Date(last.getTime() - days*86400000);
      pts = ALL.filter(function(p){{ return new Date(p[0]+'T12:00:00Z') >= cut; }});
      if (pts.length < 2) pts = ALL.slice(-2);
    }}
    var W=640, H=210, PT=14, PB=22, PL=8, PR=8;
    var vals = pts.map(function(p){{ return p[1]; }});
    var base = vals[0];
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi === lo) {{ hi += 1; lo -= 1; }}
    var pad = (hi-lo)*0.08; lo -= pad; hi += pad;
    function X(i){{ return PL + (W-PL-PR) * i / (pts.length-1); }}
    function Y(v){{ return PT + (H-PT-PB) * (1 - (v-lo)/(hi-lo)); }}
    var line = pts.map(function(p,i){{ return X(i).toFixed(1)+','+Y(p[1]).toFixed(1); }}).join(' ');
    var area = PL.toFixed(1)+','+(H-PB)+' '+line+' '+(W-PR).toFixed(1)+','+(H-PB);
    var up = vals[vals.length-1] >= base;
    var col = up ? '#60cc88' : '#ff6b6b';
    var baseY = Y(base);
    var svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" preserveAspectRatio="none" id="eqsvg">'
      + '<polygon points="'+area+'" fill="'+col+'" fill-opacity="0.10"/>'
      + '<line x1="'+PL+'" y1="'+baseY.toFixed(1)+'" x2="'+(W-PR)+'" y2="'+baseY.toFixed(1)+'" stroke="rgba(255,255,255,.16)" stroke-width="1" stroke-dasharray="3,4"/>'
      + '<polyline points="'+line+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
      + '<line id="eqx" x1="0" y1="'+PT+'" x2="0" y2="'+(H-PB)+'" stroke="rgba(255,255,255,.35)" stroke-width="1" visibility="hidden"/>'
      + '<circle id="eqdot" r="4" fill="'+col+'" stroke="#0f0f1a" stroke-width="2" visibility="hidden"/>'
      + '<text x="'+PL+'" y="'+(H-6)+'" fill="#6a6a88" font-size="10">'+esc(fmtD(pts[0][0]))+'</text>'
      + '<text x="'+(W-PR)+'" y="'+(H-6)+'" fill="#6a6a88" font-size="10" text-anchor="end">'+esc(fmtD(pts[pts.length-1][0]))+'</text>'
      + '</svg>';
    box.innerHTML = svg;
    G = {{pts:pts, X:X, Y:Y, W:W, base:base}};
    setRead(pts.length-1);
    var el = document.getElementById('eqsvg');
    function locate(clientX){{
      var r = el.getBoundingClientRect();
      var fx = (clientX - r.left) / r.width * G.W;
      var i = 0, best = 1e9;
      for (var k=0; k<G.pts.length; k++) {{
        var d = Math.abs(G.X(k)-fx);
        if (d < best) {{ best = d; i = k; }}
      }}
      return i;
    }}
    function show(i){{
      var x = G.X(i), y = G.Y(G.pts[i][1]);
      var xl = document.getElementById('eqx'), dt = document.getElementById('eqdot');
      xl.setAttribute('x1',x); xl.setAttribute('x2',x); xl.setAttribute('visibility','visible');
      dt.setAttribute('cx',x); dt.setAttribute('cy',y); dt.setAttribute('visibility','visible');
      setRead(i);
    }}
    function hide(){{
      document.getElementById('eqx').setAttribute('visibility','hidden');
      document.getElementById('eqdot').setAttribute('visibility','hidden');
      setRead(G.pts.length-1);
    }}
    el.addEventListener('mousemove', function(e){{ show(locate(e.clientX)); }});
    el.addEventListener('mouseleave', hide);
    el.addEventListener('touchstart', function(e){{ show(locate(e.touches[0].clientX)); e.preventDefault(); }}, {{passive:false}});
    el.addEventListener('touchmove',  function(e){{ show(locate(e.touches[0].clientX)); e.preventDefault(); }}, {{passive:false}});
    el.addEventListener('touchend', hide);
  }}

  var ccyBox = document.getElementById('eqccy');
  ['USD','CAD'].forEach(function(c){{
    var b = document.createElement('span');
    b.className = 'rng' + (c===CCY ? ' on' : '');
    b.textContent = (c==='CAD') ? 'CAD (native)' : 'USD';
    b.onclick = function(){{
      if (c==='CAD' && CAD.length < 2) {{
        read.textContent = 'CAD view needs the daily FX rate — recorded from now on; history backfills from IBKR.';
        return;
      }}
      CCY = c;
      Array.prototype.forEach.call(ccyBox.children, function(x){{
        x.className = 'rng' + ((x.textContent.indexOf(c)===0) === (x.textContent.indexOf(CCY)===0) && x.textContent.indexOf(CCY)===0 ? ' on' : '');
      }});
      draw();
    }};
    ccyBox.appendChild(b);
  }});
  RANGES.forEach(function(r){{
    var b = document.createElement('span');
    b.className = 'rng' + (r[0]===cur ? ' on' : '');
    b.textContent = r[0];
    b.onclick = function(){{
      cur = r[0];
      Array.prototype.forEach.call(rngs.children, function(c){{ c.className = 'rng' + (c.textContent===cur?' on':''); }});
      draw();
    }};
    rngs.appendChild(b);
  }});
  draw();
}})();
</script></body></html>"""


if __name__ == "__main__":
    p = build_pro()
    print(f"Pro dashboard written to: {p}")
