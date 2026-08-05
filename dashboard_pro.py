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
                min_trades: int = 40) -> dict[str, Any]:
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
    m = compute_metrics(closed, days_list, costs)
    grad = graduation_status(m, min_days=min_days, min_trades=min_trades)
    return {
        "trades": m["trades"], "min_trades": min_trades,
        "days": m["days"], "min_days": min_days,
        "net": m["net_pnl"], "gross": m["gross_pnl"], "costs": m["total_costs"],
        "expectancy": m["expectancy_net"] if m["trades"] else None,
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
        "graduation": _graduation(closed),
        "watchlist": watchlist,
        "equity_curve": _equity_curve(closed),
        "open_positions": _open_view(open_positions),
        "closed_summary": _closed_summary(closed),
        "journal": store.recent(limit=40),
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
    spark = _sparkline_svg(data["equity_curve"])
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
.pos{{background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.09);
border-radius:12px;padding:13px 15px;margin-bottom:10px}}
.pos .hd{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px}}
.pos .sym{{font-size:15px;font-weight:700;letter-spacing:.02em}}
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
var h = '';
h += '<div class="banner '+(D.mode==='PAPER'?'paper':'live')+'">'+esc(D.mode)+' MODE</div>';
if (D.kill_switch) h += '<div class="kill">\\u26a0\\ufe0e KILL SWITCH ENGAGED — orders blocked until cleared</div>';
var _fz = Object.keys(D.frozen||{{}});
if (_fz.length) h += '<div class="kill">\\u2744\\ufe0e FROZEN (possible assignment): '+_fz.map(esc).join(', ')+' — no automated orders until cleared (cli.py unfreeze)</div>';
else h += '<div class="killoff">Kill switch: clear — trading enabled</div>';
var iv = D.iv||{{}};
// Only the symbols WITHOUT an external benchmark rank are still bootstrapping.
// SPY/QQQ/IWM/DIA use real 252-day Cboe history, so claiming the whole system is
// waiting (and that no signals are possible) would be flatly untrue — it has
// already traded.
var boot = (D.decisions||[]).filter(function(d){{ return d.iv_rank==null; }})
                            .map(function(d){{ return d.symbol; }});
if (boot.length && (iv.days||0) < (iv.target||60)) {{
  h += '<div class="info">'+esc(boot.join(', '))+' still bootstrapping IV rank — day '+
       (iv.days||0)+' of '+(iv.target||60)+'. Those symbols stand aside; the rest use a '+
       'real 252-day benchmark rank and can trade today.</div>';
}}
h += '<h1>AiStudios</h1><div class="sub">'+esc(String(D.broker||'sim'))+' · generated '+esc((D.generated||'').slice(0,19).replace('T',' '))+' UTC</div>';
var lc = D.last_cycle;
if (lc) h += '<div class="sub" style="margin-top:-12px">last cycle '+esc(String(lc.ts||'').slice(0,16).replace('T',' '))+' · '+(lc.signals||0)+' signals · '+(lc.placed||0)+' placed · '+(lc.rejected||0)+' rejected · reconcile '+(lc.reconcile_ok?'in sync':'DRIFT')+'</div>';

h += '<div class="cards">';
h += '<div class="card"><div class="k">Equity</div><div class="v">'+money(a.equity)+'</div></div>';
h += '<div class="card"><div class="k">Buying Power</div><div class="v">'+money(a.buying_power)+'</div></div>';
h += '<div class="card"><div class="k">Cash</div><div class="v">'+money(a.cash)+'</div></div>';
h += '<div class="card"><div class="k">Net realized P&L</div><div class="v">'+signed(cs.net_pnl)+'</div></div>';
h += '</div>';

h += '<div class="sec-title">IV-rank bootstrap</div>';
var ivpct = Math.min(100, Math.round(100*(iv.days||0)/(iv.target||60)));
h += '<div class="card"><div class="gate"><span>Sessions accrued</span><b>'+(iv.days||0)+' / '+(iv.target||60)+'</b></div><div class="bar"><div class="barfill" style="width:'+ivpct+'%"></div></div><div class="muted" style="font-size:11px">Signals begin once ~'+(iv.target||60)+' daily ATM-IV readings accrue.</div></div>';

var g = D.graduation||{{}};
h += '<div class="sec-title">Graduation gate</div><div class="card">';
h += '<div class="gate"><span>Closed trades</span><b>'+(g.trades||0)+' / '+(g.min_trades||40)+'</b></div>';
h += '<div class="gate"><span>Trading days</span><b>'+(g.days||0)+' / '+(g.min_days||60)+'</b></div>';
h += '<div class="gate"><span>Net expectancy</span><b>'+(g.expectancy==null?'—':signed(g.expectancy)+' /trade')+'</b></div>';
h += '<div style="margin-top:6px;font-size:12px;color:'+(g.eligible?'#60cc88':'#ff9a6b')+'">'+(g.eligible?'Clears the minimum bar — a hurdle, not a recommendation to go live.':'Not yet eligible to consider live.')+'</div></div>';

h += '<div class="sec-title">Equity curve</div>'+curveHTML;

h += '<div class="sec-title">Closed trades</div>';
h += '<div class="cards">';
h += '<div class="card"><div class="k">Trades</div><div class="v">'+esc(cs.count||0)+'</div></div>';
h += '<div class="card"><div class="k">Win rate</div><div class="v">'+esc(cs.win_rate||0)+'%</div></div>';
h += '<div class="card"><div class="k">Wins</div><div class="v pos">'+esc(cs.wins||0)+'</div></div>';
h += '<div class="card"><div class="k">Losses</div><div class="v neg">'+esc(cs.losses||0)+'</div></div>';
h += '</div>';
var br = cs.by_reason||{{}};
var rk = Object.keys(br);
if (rk.length){{
  h += '<div class="reasons">';
  rk.forEach(function(k){{ h += '<span class="chip">'+esc(k)+' \\u00d7'+esc(br[k])+'</span>'; }});
  h += '</div>';
}}

h += '<div class="sec-title">Open positions</div>';
var ops = D.open_positions||[];
if (!ops.length) h += '<div class="empty">No open positions.</div>';
ops.forEach(function(p){{
  var lbl = p.is_credit?'credit':'debit';
  var strikes = (p.short_strike!=null?p.short_strike:'—')+'/'+(p.long_strike!=null?p.long_strike:'—');
  var u = p.unrealized;
  var pcls = (u==null)?'muted':(u>=0?'pos':'neg');
  var pval = (u==null)?'—':((u>=0?'+':'-')+'$'+Math.abs(u).toLocaleString(undefined,{{maximumFractionDigits:0}}));
  h += '<div class="pos">';
  var ptag = (p.status==='pending') ? ' <span class="tag" style="background:rgba(255,200,80,.16);color:#ffd280">FILLING</span>' : '';
  h += '<div class="hd"><span class="sym">'+esc(p.underlying||'—')+' '+esc(strikes)+
       ' <span class="tag '+(p.is_credit?'ok':'deb')+'">'+lbl+'</span>'+ptag+'</span>'+
       '<span class="pnl '+pcls+'">'+pval+'</span></div>';
  h += '<div class="grid3">'+
       '<div><div class="k">Entry</div><div class="v">'+esc(p.credit_ps)+' /sh'+
         (p.entry_fill_ps!=null?' <span class="muted">(fill '+esc(p.entry_fill_ps)+')</span>':'')+'</div></div>'+
       '<div><div class="k">Mark</div><div class="v">'+(p.mark_ps==null?'<span class="muted">—</span>':esc(p.mark_ps)+' /sh')+'</div></div>'+
       '<div><div class="k">Max loss</div><div class="v">'+money(p.max_loss)+'</div></div>'+
       '</div>';
  var tp = p.target_pct;
  if (tp!=null){{
    h += '<div class="k" style="font-size:10px;color:#8888aa">Progress to profit target</div>'+
         '<div class="prog"><div class="progf" style="width:'+Math.round(tp*100)+'%;background:'+(tp>=1?'#60cc88':'#9fb9ff')+'"></div></div>'+
         '<div class="muted" style="font-size:11px">'+Math.round(tp*100)+'% there</div>';
  }}
  var dteTxt = (p.dte==null?'—':p.dte+'d');
  var dteCls = (p.dte!=null && p.dte<=21)?'neg':'muted';
  h += '<div class="muted" style="font-size:11.5px;margin-top:7px">'+
       esc(p.contracts)+' contract'+(p.contracts==1?'':'s')+' · exp '+esc(p.expiration||'—')+
       ' · <span class="'+dteCls+'">DTE '+dteTxt+'</span>'+
       (p.entry_slip_ps!=null?' · slip '+(p.entry_slip_ps>=0?'+':'')+esc(p.entry_slip_ps)+'/sh':'')+
       (p.mark_ts?' · marked '+esc(p.mark_ts)+'Z':'')+'</div>';
  h += '</div>';
}});

// ── Execution quality: the number that decides whether edge survives costs ──
var sl = D.slippage||{{}};
if (sl.legs_measured){{
  h += '<div class="sec-title">Execution quality</div><div class="card">';
  h += '<div class="gate"><span>Mean entry slippage</span><b>'+
       (sl.mean_entry_slip_ps==null?'—':(sl.mean_entry_slip_ps>=0?'+':'')+sl.mean_entry_slip_ps+' /sh')+'</b></div>';
  if (sl.mean_exit_slip_ps!=null)
    h += '<div class="gate"><span>Mean exit slippage</span><b>'+(sl.mean_exit_slip_ps>=0?'+':'')+sl.mean_exit_slip_ps+' /sh</b></div>';
  h += '<div class="gate"><span>Worst</span><b>'+(sl.worst_slip_ps==null?'—':sl.worst_slip_ps+' /sh')+'</b></div>';
  h += '<div class="gate"><span>Coverage</span><b>'+Math.round((sl.coverage||0)*100)+'% of positions</b></div>';
  h += '<div class="muted" style="font-size:11px;margin-top:6px">Positive = worse than intended. '+
       'On a few-cents credit this is the difference between edge and none.</div>';
  if (sl.paper_fills && !sl.live_fills)
    h += '<div class="muted" style="font-size:11px;margin-top:4px">All measured fills are PAPER. '+
         'IBKR paper fills limit orders optimistically (near mid, no queue) — treat these '+
         'slippage numbers as a LOWER bound until live fills exist.</div>';
  h += '</div>';
}}

// ── Why it did / did not trade — the question worth answering daily ──
var dec = D.decisions||[];
if (dec.length){{
  h += '<div class="sec-title">Last cycle — per symbol</div><div class="card" style="padding:8px 10px">';
  h += '<table class="tbl"><tr><th>Sym</th><th>IV rank</th><th>Source</th><th>Chain</th><th>Outcome</th></tr>';
  dec.forEach(function(d){{
    var r = d.iv_rank;
    var col = (r==null)?'#7a7a98':(r>=0.40?'#60cc88':'#8888aa');
    var rtxt = (r==null)?'—':Math.round(r*100)+'%';
    h += '<tr><td><b>'+esc(d.symbol)+'</b></td>'+
         '<td><span class="dot" style="background:'+col+'"></span>'+rtxt+'</td>'+
         '<td class="muted">'+esc((d.iv_source||'').replace('cboe:',''))+'</td>'+
         '<td class="muted">'+esc(d.chain||0)+'</td>'+
         '<td class="muted">'+esc(d.note||'')+'</td></tr>';
  }});
  h += '</table></div>';
}}

var wl = D.watchlist||[];
if (wl.length){{ h += '<div class="sec-title">Watchlist</div><div class="reasons">'; wl.forEach(function(s){{ h += '<span class="chip">'+esc(s)+'</span>'; }}); h += '</div>'; }}

h += '<div class="sec-title">Recent activity</div>';
var jr = D.journal||[];
if (!jr.length) h += '<div class="empty">No journal entries yet.</div>';
jr.forEach(function(j){{
  var p = j.payload||{{}};
  var extra = Object.keys(p).slice(0,3).map(function(k){{return k+'='+p[k];}}).join(' · ');
  h += '<div class="row"><div class="top"><b>'+esc(j.kind)+'</b><span class="ts">'+esc((j.ts||'').slice(0,19).replace('T',' '))+'</span></div>'+
       '<div class="muted">'+esc(String(extra).slice(0,160))+'</div></div>';
}});

document.getElementById('app').innerHTML = h;
</script></body></html>"""


if __name__ == "__main__":
    p = build_pro()
    print(f"Pro dashboard written to: {p}")
