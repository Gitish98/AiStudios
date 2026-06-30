"""
Dashboard — emit ONE self-contained, dark, mobile-first HTML file from the
journal + account snapshot. No CDN, no fetch, no server. Open it on an iPhone.

Usage: python dashboard.py   (or `python cli.py dashboard`)
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import REPO_ROOT, load_config
from core.brokers.factory import build_execution_adapter
from core.store import Store

OUT_DIR = REPO_ROOT / "dashboard" / "out"


def build(asof: str | None = None) -> Path:
    config = load_config()
    build_res = build_execution_adapter(config, asof=asof)
    adapter = build_res.adapter
    store = Store()

    try:
        account = adapter.get_account()
        acct = {"equity": account.equity, "cash": account.cash,
                "buying_power": account.buying_power, "type": account.account_type,
                "paper": account.is_paper}
    except Exception as e:
        acct = {"error": str(e)}

    data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": "PAPER" if adapter.is_paper else "LIVE",
        "broker": adapter.name,
        "broker_note": build_res.note,
        "kill_switch": store.kill_switch,
        "account": acct,
        "orders": store.all_orders(limit=40),
        "journal": store.recent(limit=40),
    }
    store.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "dashboard.html"
    out.write_text(_render(data), encoding="utf-8")
    return out


def _render(data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>AiStudios — Paper Dashboard</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
background:#0f0f1a;color:#e8e8f4;padding:16px;padding-bottom:40px;-webkit-text-size-adjust:100%}}
.banner{{border-radius:12px;padding:14px 16px;margin-bottom:14px;font-weight:700;letter-spacing:.05em}}
.paper{{background:rgba(80,200,120,.15);color:#60cc88;border:1px solid rgba(80,200,120,.3)}}
.live{{background:rgba(255,80,80,.15);color:#ff6b6b;border:1px solid rgba(255,80,80,.4)}}
.kill{{background:rgba(255,180,40,.14);color:#ffb648;border:1px solid rgba(255,180,40,.35);
border-radius:10px;padding:10px 14px;margin-bottom:14px;font-weight:600;font-size:13px}}
h1{{font-size:18px;margin-bottom:2px}}
.sub{{color:#8888aa;font-size:12px;margin-bottom:16px}}
.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:18px}}
.card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
border-radius:12px;padding:14px}}
.card .k{{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#8888aa}}
.card .v{{font-size:20px;font-weight:700;margin-top:4px}}
.sec-title{{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:#8888aa;margin:18px 0 10px}}
.note{{background:rgba(108,108,255,.1);border:1px solid rgba(108,108,255,.2);border-radius:10px;
padding:10px 12px;font-size:12px;color:#b8b8e8;margin-bottom:14px}}
.row{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:10px;
padding:11px 13px;margin-bottom:8px;font-size:13px}}
.row .top{{display:flex;justify-content:space-between;gap:8px;margin-bottom:3px}}
.tag{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
padding:2px 7px;border-radius:20px}}
.ok{{background:rgba(80,200,120,.16);color:#60cc88}}
.rej{{background:rgba(255,80,80,.14);color:#ff8a8a}}
.muted{{color:#7a7a98}}
.ts{{color:#6a6a88;font-size:11px}}
.empty{{color:#7a7a98;font-size:13px;padding:14px 0}}
</style></head><body>
<div id="app"></div>
<script>
var D = {payload};
function esc(s){{return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function money(v){{var n=Number(v); return isNaN(n)?'—':'$'+n.toLocaleString(undefined,{{maximumFractionDigits:0}});}}
var a = D.account||{{}};
var h = '';
h += '<div class="banner '+(D.mode==='PAPER'?'paper':'live')+'">'+esc(D.mode)+' · '+esc(D.broker)+'</div>';
if (D.kill_switch) h += '<div class="kill">⚠︎ KILL SWITCH ENGAGED — orders are blocked until cleared (python cli.py clear-kill)</div>';
h += '<h1>AiStudios</h1><div class="sub">Paper dashboard · generated '+esc((D.generated||'').slice(0,19).replace('T',' '))+' UTC</div>';
if (D.broker_note) h += '<div class="note">'+esc(D.broker_note)+'</div>';
h += '<div class="cards">';
h += '<div class="card"><div class="k">Equity</div><div class="v">'+(a.error?'—':money(a.equity))+'</div></div>';
h += '<div class="card"><div class="k">Buying Power</div><div class="v">'+(a.error?'—':money(a.buying_power))+'</div></div>';
h += '<div class="card"><div class="k">Account</div><div class="v" style="font-size:15px">'+esc(a.type||'—')+(a.paper?' · paper':'')+'</div></div>';
h += '<div class="card"><div class="k">Orders logged</div><div class="v">'+((D.orders||[]).length)+'</div></div>';
h += '</div>';

h += '<div class="sec-title">Orders</div>';
if (!(D.orders||[]).length) h += '<div class="empty">No orders yet. Run a cycle: <b>python cli.py run-cycle</b></div>';
(D.orders||[]).forEach(function(o){{
  var st = (o.status||'').toLowerCase();
  var cls = (st==='rejected')?'rej':'ok';
  h += '<div class="row"><div class="top"><b>'+esc(o.underlying||o.strategy||'order')+'</b>'+
       '<span class="tag '+cls+'">'+esc(o.status||'—')+'</span></div>'+
       '<div class="muted">'+esc(o.strategy||'')+' · '+esc((o.client_order_id||'').slice(0,16))+'</div>'+
       '<div class="ts">'+esc((o.ts||'').slice(0,19).replace('T',' '))+'</div></div>';
}});

h += '<div class="sec-title">Recent activity</div>';
if (!(D.journal||[]).length) h += '<div class="empty">No journal entries yet.</div>';
(D.journal||[]).forEach(function(j){{
  var p = j.payload||{{}};
  var extra = '';
  if (j.kind==='risk_decision') extra = (p.approved?'approved':'rejected: '+(p.reasons||[]).join('; '));
  else if (j.kind==='order_placed') extra = (p.status||'')+' · '+esc(p.rationale||'');
  else extra = Object.keys(p).slice(0,3).map(function(k){{return k+'='+p[k];}}).join(' · ');
  h += '<div class="row"><div class="top"><b>'+esc(j.kind)+'</b><span class="ts">'+esc((j.ts||'').slice(11,19))+'</span></div>'+
       '<div class="muted">'+esc(String(extra).slice(0,160))+'</div></div>';
}});

document.getElementById('app').innerHTML = h;
</script></body></html>"""


if __name__ == "__main__":
    p = build()
    print(f"Dashboard written to: {p}")
