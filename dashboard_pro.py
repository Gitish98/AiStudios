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
        })
    return out


def _gather(store: Store) -> dict[str, Any]:
    closed = store.get_closed_positions()
    open_positions = store.get_open_positions()

    # Equity: derive from store kv markers if present; otherwise leave None.
    equity = store.get_kv("equity")
    cash = store.get_kv("cash")
    buying_power = store.get_kv("buying_power")

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": "PAPER",
        "kill_switch": store.kill_switch,
        "account": {
            "equity": _num(equity) if equity is not None else None,
            "cash": _num(cash) if cash is not None else None,
            "buying_power": _num(buying_power) if buying_power is not None else None,
        },
        "equity_curve": _equity_curve(closed),
        "open_positions": _open_view(open_positions),
        "closed_summary": _closed_summary(closed),
        "journal": store.recent(limit=40),
    }


def build_pro(store_path: Optional[Path] = None) -> Path:
    store = Store(Path(store_path) if store_path else None)
    try:
        data = _gather(store)
    finally:
        store.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "dashboard_pro.html"
    out.write_text(_render(data))
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
else h += '<div class="killoff">Kill switch: clear — trading enabled</div>';
h += '<h1>AiStudios Pro</h1><div class="sub">generated '+esc((D.generated||'').slice(0,19).replace('T',' '))+' UTC</div>';

h += '<div class="cards">';
h += '<div class="card"><div class="k">Equity</div><div class="v">'+money(a.equity)+'</div></div>';
h += '<div class="card"><div class="k">Buying Power</div><div class="v">'+money(a.buying_power)+'</div></div>';
h += '<div class="card"><div class="k">Cash</div><div class="v">'+money(a.cash)+'</div></div>';
h += '<div class="card"><div class="k">Net realized P&L</div><div class="v">'+signed(cs.net_pnl)+'</div></div>';
h += '</div>';

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
  var cls = p.is_credit?'ok':'deb';
  var lbl = p.is_credit?'credit':'debit';
  var strikes = (p.short_strike!=null?p.short_strike:'—')+' / '+(p.long_strike!=null?p.long_strike:'—');
  h += '<div class="row"><div class="top"><b>'+esc(p.underlying||'—')+' · '+esc(p.structure||'')+'</b>'+
       '<span class="tag '+cls+'">'+lbl+'</span></div>'+
       '<div class="muted">strikes '+esc(strikes)+' · '+esc(p.contracts)+'x · DTE '+esc(p.dte==null?'—':p.dte)+' · exp '+esc(p.expiration||'—')+'</div>'+
       '<div class="muted">'+lbl+' '+esc(p.credit_ps)+' /sh · max loss '+money(p.max_loss)+'</div></div>';
}});

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
