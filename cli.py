#!/usr/bin/env python3
"""
AiStudios — single entrypoint. Designed for one-command operation from a phone.

  python cli.py status        plain-text summary (mode, account, orders, kill switch)
  python cli.py run-cycle     run ONE paper cycle (data -> signals -> risk -> orders)
  python cli.py dry-run       same, but never places orders (shows what WOULD happen)
  python cli.py dashboard     rebuild the self-contained HTML dashboard
  python cli.py reconcile     compare broker truth vs local journal
  python cli.py kill          engage the kill switch (gate rejects everything)
  python cli.py clear-kill    clear the kill switch
  python cli.py go-live       refused in Phase 0 (paper only)

Everything is PAPER. There is no path to live trading in this file.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from core.config import load_config
from core.brokers.factory import build_execution_adapter
from core.store import Store
from core.execution import run_cycle
from strategies.premium_harvest import PremiumHarvest


def _strategies(config):
    s = config.strategies or {}
    out = []
    ph = s.get("premium_harvest")
    if ph is None or ph.get("enabled", True):
        out.append(PremiumHarvest(ph or {}))
    vb = s.get("volatility_breakout")
    if vb is not None and vb.get("enabled", True):
        from strategies.breakout import VolatilityBreakout
        out.append(VolatilityBreakout(vb))
    ev = s.get("earnings_vol")
    if ev is not None and ev.get("enabled", True):
        from strategies.earnings import EarningsVol
        out.append(EarningsVol(ev))
    return out


def _banner(adapter) -> str:
    mode = "PAPER" if adapter.is_paper else "LIVE"
    return f"  MODE: {mode}   BROKER: {adapter.name}"


def cmd_status(args):
    config = load_config()
    build_res = build_execution_adapter(config, asof=args.asof)
    adapter = build_res.adapter
    store = Store()

    print("─" * 56)
    print(_banner(adapter))
    print("─" * 56)
    print(f"  {build_res.note}")
    from core.execution import capability_warnings
    for w in capability_warnings(adapter, config):
        print(f"  ⚠️  {w}")
    if store.kill_switch:
        print("\n  ⚠️  KILL SWITCH ENGAGED — orders are blocked. Clear with: python cli.py clear-kill")

    try:
        a = adapter.get_account()
        print(f"\n  Account type     : {a.account_type}")
        print(f"  Equity           : ${a.equity:,.2f}")
        print(f"  Cash             : ${a.cash:,.2f}")
        print(f"  Buying power     : ${a.buying_power:,.2f}")
        print(f"  Day trades (5d)  : {a.pdt_day_trade_count}")
    except Exception as e:
        print(f"\n  Account: unavailable ({e})")

    open_pos = store.get_open_positions()
    print(f"\n  Open positions   : {len(open_pos)} (tracked)")
    for p in open_pos[:12]:
        from core.positions import dte_from
        dte = dte_from(p["expiration"], datetime.now(timezone.utc).date().isoformat())
        fam = (p.get("family") or "put").upper()[0]
        net = "credit" if p.get("is_credit", 1) else "debit"
        print(f"    {p['underlying']:<5} {p['short_strike']:g}/{p['long_strike']:g}{fam} "
              f"{p['expiration']} ({dte}d)  {net} ${p['entry_credit_ps']*100:.0f}  "
              f"maxloss ${p['max_loss']:.0f}")

    orders = store.all_orders(limit=8)
    print(f"\n  Recent orders    : {len(store.all_orders(limit=200))} total")
    for o in orders:
        print(f"    {str(o['ts'])[:19]}  {str(o.get('underlying') or ''):<6} "
              f"{str(o.get('strategy') or ''):<18} {o.get('status')}")
    from core.reconcile import reconcile
    rec = reconcile(adapter, store)
    if rec.get("note"):
        print(f"\n  Reconcile        : {rec['note']}")
    elif rec["ok"]:
        print(f"\n  Reconcile        : ✓ in sync with broker")
    else:
        print(f"\n  Reconcile        : ⚠️  {len(rec['drift'])} drift item(s) — run: python cli.py reconcile")

    print(f"\n  Watchlist        : {', '.join(config.watchlist) or '(empty)'}")
    store.close()


def cmd_run_cycle(args, dry_run=False):
    config = load_config()

    # Market-calendar guard (unless --force). Honors --asof for deterministic runs.
    from datetime import date as _date
    from core.market_calendar import is_trading_day, trading_day_reason
    check_day = _date.fromisoformat(args.asof) if args.asof else None
    enforce = (config.raw.get("market", {}) or {}).get("enforce_calendar", True)
    if enforce and not getattr(args, "force", False) and not is_trading_day(check_day):
        print(f"  Skipping cycle — {trading_day_reason(check_day)}. "
              f"Use --force to override.")
        return

    build_res = build_execution_adapter(config, asof=args.asof)
    adapter = build_res.adapter
    store = Store()
    print(_banner(adapter))
    print(f"  {build_res.note}\n")

    summary = run_cycle(adapter, _strategies(config), store, config,
                        asof=args.asof, dry_run=dry_run)

    for w in summary.get("warnings", []):
        print(f"  ⚠️  {w}")
    m = summary.get("manage", {})
    if m.get("evaluated"):
        print(f"  Management: {m['evaluated']} open · closed {len(m.get('closed', []))} "
              f"· realized today ${m.get('realized_today', 0):+.0f} "
              f"· unrealized ${m.get('unrealized_open', 0):+.0f}")
        for c in m.get("closed", []):
            print(f"    ↩ {c['underlying']} {c['reason']} → ${c['realized_pnl']:+.0f}")
    print(f"  Daily P&L  : ${summary.get('daily_pnl', 0):+.0f}")

    print(f"  Cycle {summary['asof']} — {summary['broker']} "
          f"({'DRY-RUN' if dry_run else summary['mode']})")
    print(f"  Signals generated : {len(summary['signals'])}")
    for s in summary["signals"]:
        print(f"    • {s['rationale']}")
    print(f"\n  Placed   : {len(summary['placed'])}")
    for p in summary["placed"]:
        tag = " [DRY]" if p.get("dry_run") else ""
        print(f"    ✓ {p['underlying']} {p['strategy']} "
              f"(credit ${p.get('est_credit', 0):.0f}){tag}")
    print(f"  Rejected : {len(summary['rejected'])}")
    for r in summary["rejected"]:
        print(f"    ✗ {r['underlying']} {r['strategy']}: {'; '.join(r['reasons'])}")
    if summary["skipped_duplicates"]:
        print(f"  Skipped (already placed today): {len(summary['skipped_duplicates'])}")
    if summary.get("kill_switch"):
        print("\n  ⚠️  Kill switch is now ENGAGED.")
    store.close()


def cmd_dashboard(args):
    import dashboard
    p = dashboard.build(asof=args.asof)
    print(f"Dashboard written to: {p}")
    print("Open it in any browser (works on iPhone).")


def cmd_kill(args):
    store = Store()
    store.set_kill_switch(True)
    store.append(datetime.now(timezone.utc).isoformat(), "kill_switch", {"engaged": True})
    print("Kill switch ENGAGED. All orders will be rejected until cleared.")
    store.close()


def cmd_clear_kill(args):
    store = Store()
    store.set_kill_switch(False)
    store.append(datetime.now(timezone.utc).isoformat(), "kill_switch", {"engaged": False})
    print("Kill switch CLEARED.")
    store.close()


def cmd_reconcile(args):
    from datetime import datetime, timezone
    from core.reconcile import reconcile
    config = load_config()
    build_res = build_execution_adapter(config, asof=args.asof)
    adapter = build_res.adapter
    store = Store()
    print(_banner(adapter))

    report = reconcile(adapter, store)
    print(f"  Tracked open legs source : {report['tracked_open']} position(s)")
    if report.get("note"):
        print(f"  Note: {report['note']}")
    if report["ok"]:
        print("  ✓ In sync — no drift between our book and the broker.")
    else:
        print(f"  ⚠️  DRIFT DETECTED ({len(report['drift'])} item(s)):")
        for d in report["drift"]:
            if d["kind"] == "broker_unreachable":
                print(f"    ✗ broker unreachable: {d['detail']}")
            else:
                print(f"    ✗ {d['kind']}: {d['leg']} "
                      f"(we={d['expected_qty']:+g}, broker={d['broker_qty']:+g})")
        # Surface loudly + journal. Optionally trip the kill switch.
        store.append(datetime.now(timezone.utc).isoformat(), "reconcile_drift",
                     {"drift": report["drift"]})
        if (config.raw.get("reconcile", {}) or {}).get("auto_kill_on_drift", False):
            store.set_kill_switch(True)
            print("\n  Kill switch ENGAGED (reconcile.auto_kill_on_drift = true).")
        else:
            print("\n  Investigate before trading. Trip the kill switch with: "
                  "python cli.py kill")
    store.close()


def cmd_go_live(args):
    print("go-live is DISABLED in Phase 0. This system is paper-only.")
    print("See docs/05-risk-and-safety.md (§1) and docs/07-roadmap.md for the")
    print("staged path to a guarded, small-size live mode.")
    sys.exit(2)


def main():
    p = argparse.ArgumentParser(prog="aistudios", description="AiStudios paper trading CLI")
    p.add_argument("--asof", help="Override the as-of date (YYYY-MM-DD) for deterministic runs")
    p.add_argument("--force", action="store_true",
                   help="ignore the market-calendar guard (run even on a closed day)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Plain-text account/order summary").set_defaults(func=cmd_status)
    sub.add_parser("run-cycle", help="Run one paper cycle").set_defaults(func=cmd_run_cycle)
    sub.add_parser("dry-run", help="Run a cycle without placing orders").set_defaults(
        func=lambda a: cmd_run_cycle(a, dry_run=True))
    sub.add_parser("dashboard", help="Rebuild the HTML dashboard").set_defaults(func=cmd_dashboard)
    sub.add_parser("reconcile", help="Diff broker vs local journal").set_defaults(func=cmd_reconcile)
    sub.add_parser("kill", help="Engage the kill switch").set_defaults(func=cmd_kill)
    sub.add_parser("clear-kill", help="Clear the kill switch").set_defaults(func=cmd_clear_kill)
    sub.add_parser("go-live", help="(disabled in Phase 0)").set_defaults(func=cmd_go_live)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
