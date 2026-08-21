"""
Performance metrics + graduation gate + per-trade rows — shared by the backtest,
the paper-performance tracker, and the tax/trade-log export, so all three report
the SAME cost-adjusted numbers.

Graduation gate (the honest bar before any live consideration): enough out-of-
sample paper history AND a positive NET (after-cost) expectancy. A high win rate
is explicitly NOT sufficient.
"""

from __future__ import annotations

from typing import Any, Optional

from .costs import CostModel

GRAD_MIN_DAYS = 60
GRAD_MIN_TRADES = 40


def paper_record_sessions(store) -> int:
    """TRADING SESSIONS elapsed since the strategy started trading — the paper
    record's length.

    Why this is not "distinct dates on which a trade closed", which is what the
    gate used to receive: that count is bounded above by the number of trades, so
    requiring 60 of them implied 60+ closes and made the separate "40 closed
    trades" condition DEAD — it could never bind. The two hurdles are meant to be
    independent: enough ELAPSED TIME (market regimes) and enough SAMPLE (trades).

    Sessions come from the IV-snapshot record, which is exactly the set of days a
    cycle actually ran, counted from the first position ever opened (the record
    begins when the strategy starts trading, not when the VM was provisioned)."""
    try:
        first = store.conn.execute(
            "SELECT MIN(opened_asof) FROM positions").fetchone()[0]
        if not first:
            return 0
        return int(store.conn.execute(
            "SELECT COUNT(DISTINCT asof) FROM iv_snapshots WHERE asof >= ?",
            (first,)).fetchone()[0] or 0)
    except Exception:
        return 0


def compute_metrics(closed: list[dict], days: list[str],
                    costs: Optional[CostModel] = None,
                    sessions: Optional[int] = None) -> dict[str, Any]:
    costs = costs or CostModel()
    gross = [float(p.get("realized_pnl") or 0.0) for p in closed]
    # Slippage is MEASURED per position where the broker reported a fill, modeled
    # otherwise. This is what makes realized_pnl (computed from INTENDED prices)
    # honest: gross_from_intent - measured_slippage == gross_from_actual_fills.
    trade_costs = [costs.position_cost(p.get("structure", "put_credit_spread"),
                                       int(p.get("contracts") or 1),
                                       p.get("exit_reason", ""),
                                       p.get("entry_slip_ps"),
                                       p.get("exit_slip_ps"),
                                       p.get("width")) for p in closed]
    _cov = [costs.measured_dollars(p, p.get("exit_reason", "")) for p in closed]
    _m, _a = sum(x for x, _ in _cov), sum(y for _, y in _cov)
    pnls = [g - c for g, c in zip(gross, trade_costs)]
    total_costs = round(sum(trade_costs), 2)
    n = len(pnls)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    net = round(sum(pnls), 2)

    cum, peak, max_dd = 0.0, 0.0, 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_reason: dict[str, int] = {}
    for p in closed:
        r = p.get("exit_reason", "?")
        by_reason[r] = by_reason.get(r, 0) + 1

    gross_net = round(sum(gross), 2)
    return {
        # `days` is the paper record's LENGTH in trading sessions when the caller
        # supplies it; the legacy closing-date count is kept as a fallback for
        # callers (and the backtest) that have no session record.
        "days": int(sessions) if sessions is not None else len(days),
        "closing_days": len(days), "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "gross_pnl": gross_net, "total_costs": total_costs, "net_pnl": net,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy_net": round(net / n, 2) if n else 0.0,
        "cost_per_trade": round(total_costs / n, 2) if n else 0.0,
        # How much of the cost figure is EVIDENCE vs model. A low number means
        # the expectancy below still rests largely on assumptions.
        "cost_measured_pct": round(_m / _a, 3) if _a else 0.0,
        "cost_drag_pct": round(total_costs / abs(gross_net), 4) if gross_net else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 2),
        "exits_by_reason": by_reason,
        "note": "Net of commissions + slippage (MEASURED per fill where the broker "
                "reported one, modeled otherwise — see cost_measured_pct). "
                "Option prices are MODELED (BS), "
                "not real fills. Necessary, not sufficient — paper-trade before live.",
    }


def graduation_status(metrics: dict, min_days: int = GRAD_MIN_DAYS,
                      min_trades: int = GRAD_MIN_TRADES) -> dict:
    """Whether the paper record clears the bar to even CONSIDER live. Never a
    recommendation to go live — just a minimum hurdle."""
    reasons = []
    if metrics["days"] < min_days:
        reasons.append(f"only {metrics['days']}/{min_days} trading sessions "
                       "of paper record")
    if metrics["trades"] < min_trades:
        reasons.append(f"only {metrics['trades']}/{min_trades} closed trades")
    if metrics["expectancy_net"] <= 0:
        reasons.append(f"net expectancy ${metrics['expectancy_net']:.2f}/trade is not positive")
    return {"graduated": not reasons, "reasons": reasons}


def trade_rows(closed: list[dict], costs: Optional[CostModel] = None) -> list[dict]:
    """One row per closed trade, cost-adjusted — for the tax/trade-log CSV."""
    costs = costs or CostModel()
    rows = []
    for p in closed:
        contracts = int(p.get("contracts") or 1)
        gross = float(p.get("realized_pnl") or 0.0)
        # Same measured-slippage treatment as compute_metrics — the tax/trade-log
        # CSV and the performance summary must never disagree about cost.
        cost = costs.position_cost(p.get("structure", "put_credit_spread"),
                                   contracts, p.get("exit_reason", ""),
                                   p.get("entry_slip_ps"), p.get("exit_slip_ps"),
                                   p.get("width"))
        _md, _td = costs.measured_dollars(p, p.get("exit_reason", ""))
        rows.append({
            "opened": p.get("opened_asof", ""), "closed": p.get("closed_asof", ""),
            "underlying": p.get("underlying", ""), "structure": p.get("structure", ""),
            "contracts": contracts,
            "short_strike": p.get("short_strike", ""), "long_strike": p.get("long_strike", ""),
            "expiration": p.get("expiration", ""),
            "entry_credit": round(float(p.get("entry_credit_ps") or 0) * 100 * contracts, 2),
            "exit_reason": p.get("exit_reason", ""),
            "gross_pnl": round(gross, 2), "cost": round(cost, 2),
            "net_pnl": round(gross - cost, 2),
            "entry_slip_ps": p.get("entry_slip_ps"), "exit_slip_ps": p.get("exit_slip_ps"),
            "cost_measured_pct": round(_md / _td, 3) if _td else 0.0,
        })
    return rows
