# 13 — Backtesting

> Goal: explain how `backtest.py` replays the **real** trading cycle over
> historical bars, the metrics it reports, and the honest limitation that
> option prices are **modeled**, not real fills. Still paper. Not
> financial/legal/tax advice.

---

## What it does

`backtest.py` is a **replay harness**, not a separate backtest engine. It runs
the **exact same `run_cycle`** used in live/paper trading, day by day, over a
window of historical trading days. The risk gate, fill lifecycle, position
management, and P&L math are **identical to production** — so there is no
divergent backtest code to drift out of sync with reality.

```
from backtest import run_backtest
report = run_backtest(strategies, bars_by_symbol, config, start, end)
```

---

## How the replay works

A `BacktestBroker` (subclass of `SimAdapter`) drives the cycle:

- It steps an `asof` date across every trading day in `[start, end]`.
- For each day it calls `run_cycle(broker, strategies, store, config, asof=day)`.
- Spot, history, IV, and earnings are sourced from the **real bar data you
  feed in**, sliced so the strategy only ever sees bars **at or before** the
  current `asof` (no lookahead).
- Where IV isn't supplied, a **realized-vol proxy** stands in (honest model),
  and IV history is a rolling realized-vol series so IV rank is computable.
- Fills are instant; the broker name is `sim` so reconcile no-ops.
- A throwaway temp `Store` records positions; closed positions feed the metrics.

---

## Metrics it reports

`run_backtest` returns a dict:

| Field | Meaning |
|---|---|
| `days`, `trades` | trading days replayed; number of closed trades |
| `wins`, `losses`, `win_rate` | counts and win fraction |
| `net_pnl` | total realized P&L |
| `avg_win`, `avg_loss` | mean win / mean loss |
| `expectancy` | net P&L per trade |
| `profit_factor` | gross profit / gross loss (`None` if no losses) |
| `max_drawdown` | worst peak-to-trough on the cumulative realized curve |
| `exits_by_reason` | trade count bucketed by exit reason |
| `note` | the standing reminder that prices are modeled |

---

## The honest limitation

> **Option prices here are MODELED (Black-Scholes from the day's close plus an
> IV path), not real historical option quotes.**

Equity bar history is real (or whatever you feed it), but the **option chain is
a model**. So a backtest measures whether the **strategy logic** has an edge
under a clean model. It does **not** capture:

- real bid/ask spreads or slippage,
- actual fills (you may not get the modeled price),
- early assignment,
- IV surface skew and term structure.

**A positive backtest is necessary, not sufficient.** Treat it as a logic check,
then **paper-trade before any live capital.**
