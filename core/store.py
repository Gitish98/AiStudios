"""
State store — SQLite, the single source of local truth.

Append-only journal of everything that happens, plus an orders table and a
key/value table (kill switch, daily P&L marker). Local file, gitignored.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .config import REPO_ROOT

DEFAULT_DB = REPO_ROOT / "data" / "portfolio.db"

# Canonical (lowercased) terminal/dead order statuses across ALL brokers. An
# order in one of these states is finished and must NOT block re-entry of the
# same idempotency key. Used by has_order, _count_opened_today, and the
# pending-order finalizers so the canonicalization can't drift between them.
DEAD_ORDER_STATUSES = (
    "rejected", "canceled", "cancelled", "inactive", "apicancelled", "expired",
)


class Store:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                broker_order_id TEXT,
                status TEXT,
                strategy TEXT,
                underlying TEXT,
                filled_qty REAL,
                filled_avg_price REAL,
                payload TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS iv_snapshots (
                symbol TEXT NOT NULL,
                asof TEXT NOT NULL,
                atm_iv REAL NOT NULL,
                PRIMARY KEY (symbol, asof)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT,
                strategy TEXT,
                structure TEXT,             -- put_credit_spread | call_debit_spread | ...
                family TEXT,                -- put | call
                is_credit INTEGER,          -- 1 credit, 0 debit
                underlying TEXT,
                status TEXT,                -- open | closed
                opened_asof TEXT,
                opened_ts TEXT,
                expiration TEXT,
                contracts INTEGER,
                short_strike REAL,
                long_strike REAL,
                width REAL,
                legs_json TEXT,             -- full leg detail for multi-leg structures (iron condor)
                entry_credit_ps REAL,       -- per-share net credit (e.g. 0.26)
                max_loss REAL,              -- dollars
                closed_asof TEXT,
                closed_ts TEXT,
                exit_reason TEXT,
                exit_value_ps REAL,
                realized_pnl REAL,          -- dollars (from INTENDED prices)
                -- Realized execution economics. Captured at fill time because IB's
                -- trades() feed is session-scoped: a fill price not recorded the
                -- same day is gone forever. Without these, every P&L number is a
                -- restatement of our own mid-price assumptions. See core/fills.py.
                entry_fill_ps REAL,         -- broker fill, our signed-credit convention
                exit_fill_ps REAL,
                entry_slip_ps REAL,         -- + = adverse vs intent
                exit_slip_ps REAL
            )""")
        # Migrate existing DBs (the VM has months of rows) — add any missing column.
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        for col in ("entry_fill_ps", "exit_fill_ps", "entry_slip_ps", "exit_slip_ps"):
            if col not in have:
                c.execute(f"ALTER TABLE positions ADD COLUMN {col} REAL")
        self.conn.commit()

    # ── journal ──────────────────────────────────────────────────────────────
    def append(self, ts: str, kind: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO journal (ts, kind, payload) VALUES (?, ?, ?)",
            (ts, kind, json.dumps(payload, default=str)),
        )
        self.conn.commit()

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, kind, payload FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload"])}
            for r in rows
        ]

    # ── orders ───────────────────────────────────────────────────────────────
    def has_order(self, client_order_id: str) -> bool:
        """True only for a LIVE/accepted prior order. A dead order (rejected/
        canceled/inactive/... in any broker's spelling/case) must not permanently
        block a retry of the same signal."""
        ph = ",".join("?" * len(DEAD_ORDER_STATUSES))
        return self.conn.execute(
            f"SELECT 1 FROM orders WHERE client_order_id = ? "
            f"AND LOWER(status) NOT IN ({ph})",
            (client_order_id, *DEAD_ORDER_STATUSES),
        ).fetchone() is not None

    def set_order_status(self, client_order_id: str, status: str) -> None:
        """Reconcile the orders table to a broker-terminal status (so has_order /
        _count_opened_today see the order as dead and allow re-entry)."""
        self.conn.execute("UPDATE orders SET status = ? WHERE client_order_id = ?",
                          (status, client_order_id))
        self.conn.commit()

    def record_order(self, ts: str, order_req: Any, result: Any) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO orders
               (client_order_id, ts, broker_order_id, status, strategy, underlying,
                filled_qty, filled_avg_price, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.client_order_id, ts, result.broker_order_id, result.status,
                getattr(order_req, "strategy", ""), getattr(order_req, "underlying", ""),
                result.filled_qty, result.filled_avg_price,
                json.dumps({"request": asdict(order_req), "result": asdict(result)}, default=str),
            ),
        )
        self.conn.commit()

    def open_orders(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('new','accepted','partially_filled') ORDER BY ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def all_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── IV-rank history bootstrap ────────────────────────────────────────────
    def record_iv_snapshot(self, symbol: str, asof: str, atm_iv: float) -> None:
        """One ATM-IV reading per (symbol, day). Over time these BUILD the IV-rank
        history that premium-harvest / earnings-vol need — no paid feed required.
        Idempotent per day."""
        if atm_iv is None or atm_iv <= 0:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO iv_snapshots (symbol, asof, atm_iv) VALUES (?, ?, ?)",
            (symbol.upper(), asof, float(atm_iv)))
        self.conn.commit()

    def get_iv_history(self, symbol: str, limit: int = 252) -> list[float]:
        """ATM-IV readings for a symbol, oldest→newest (up to `limit` most recent)."""
        rows = self.conn.execute(
            "SELECT atm_iv FROM iv_snapshots WHERE symbol = ? ORDER BY asof DESC LIMIT ?",
            (symbol.upper(), limit)).fetchall()
        return [float(r["atm_iv"]) for r in reversed(rows)]

    # ── key/value (kill switch, markers) ─────────────────────────────────────
    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    # ── positions ────────────────────────────────────────────────────────────
    def open_position(self, pos: dict[str, Any]) -> int:
        cols = ("client_order_id", "strategy", "structure", "family", "is_credit",
                "underlying", "status", "opened_asof",
                "opened_ts", "expiration", "contracts", "short_strike", "long_strike",
                "width", "legs_json", "entry_credit_ps", "max_loss")
        cur = self.conn.execute(
            f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
            tuple(pos.get(c) for c in cols),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_open_positions(self) -> list[dict[str, Any]]:
        """FILLED positions only (status='open'). Used by management and reconcile
        — we must not manage or expect-at-broker an entry that hasn't filled."""
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_positions(self) -> list[dict[str, Any]]:
        """Filled (open) AND working (pending) positions — committed risk the gate
        must account for so accepted-but-unfilled entries can't be over-committed."""
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'pending') ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'closed' ORDER BY closed_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def record_entry_fill(self, pos_id: int, fill_ps: Optional[float],
                          slip_ps: Optional[float]) -> None:
        """Persist realized ENTRY execution. Called the moment a fill is observed —
        the broker's fill feed is session-scoped, so this is a one-shot capture."""
        if fill_ps is None and slip_ps is None:
            return
        self.conn.execute(
            "UPDATE positions SET entry_fill_ps = ?, entry_slip_ps = ? WHERE id = ?",
            (fill_ps, slip_ps, pos_id))
        self.conn.commit()

    def record_exit_fill(self, pos_id: int, fill_ps: Optional[float],
                         slip_ps: Optional[float]) -> None:
        """Persist realized EXIT execution (see record_entry_fill)."""
        if fill_ps is None and slip_ps is None:
            return
        self.conn.execute(
            "UPDATE positions SET exit_fill_ps = ?, exit_slip_ps = ? WHERE id = ?",
            (fill_ps, slip_ps, pos_id))
        self.conn.commit()

    def set_position_status(self, pos_id: int, status: str) -> None:
        self.conn.execute("UPDATE positions SET status = ? WHERE id = ?", (status, pos_id))
        self.conn.commit()

    def delete_position(self, pos_id: int) -> None:
        self.conn.execute("DELETE FROM positions WHERE id = ?", (pos_id,))
        self.conn.commit()

    def close_position(self, pos_id: int, closed_asof: str, closed_ts: str,
                       exit_reason: str, exit_value_ps: float, realized_pnl: float) -> None:
        self.conn.execute(
            """UPDATE positions SET status='closed', closed_asof=?, closed_ts=?,
               exit_reason=?, exit_value_ps=?, realized_pnl=? WHERE id=?""",
            (closed_asof, closed_ts, exit_reason, exit_value_ps, realized_pnl, pos_id),
        )
        self.conn.commit()

    def realized_pnl_on(self, asof: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM positions "
            "WHERE status='closed' AND closed_asof = ?", (asof,)
        ).fetchone()
        return float(row["s"] or 0.0)

    @property
    def kill_switch(self) -> bool:
        return self.get_kv("kill_switch", "0") == "1"

    def set_kill_switch(self, on: bool) -> None:
        self.set_kv("kill_switch", "1" if on else "0")

    def close(self) -> None:
        self.conn.close()
