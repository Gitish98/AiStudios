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
                entry_credit_ps REAL,       -- per-share net credit (e.g. 0.26)
                max_loss REAL,              -- dollars
                closed_asof TEXT,
                closed_ts TEXT,
                exit_reason TEXT,
                exit_value_ps REAL,
                realized_pnl REAL           -- dollars
            )""")
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
        """True only for a LIVE/accepted prior order. A broker-rejected or
        canceled order must not permanently block a retry of the same signal."""
        return self.conn.execute(
            "SELECT 1 FROM orders WHERE client_order_id = ? "
            "AND status NOT IN ('rejected', 'canceled')",
            (client_order_id,),
        ).fetchone() is not None

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
                "width", "entry_credit_ps", "max_loss")
        cur = self.conn.execute(
            f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
            tuple(pos.get(c) for c in cols),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

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
