"""
State persistence layer.

The #1 cause of blown-up retail trading bots: state lives only in memory, the process
crashes or the VPS reboots, and the bot comes back up with consecutive_losses=0 and
no memory of open positions — so it happily starts trading again right through a
circuit breaker that should still be active.

Everything risk-critical is written to SQLite on every state change (not batched),
using atomic transactions. A JSON snapshot is also written after every trade for
human-readable backups and for the local watchdog to inspect without needing to
speak SQL.
"""
import json
import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "state.db"
SNAPSHOT_PATH = Path(__file__).parent.parent / "data" / "state_snapshot.json"
BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    trading_balance REAL NOT NULL,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    daily_pnl REAL NOT NULL DEFAULT 0,
    daily_reset_at TEXT NOT NULL,
    trading_halted INTEGER NOT NULL DEFAULT 0,
    halt_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    entry_price REAL,
    exit_price REAL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS open_positions (
    client_order_id TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss_price REAL,
    take_profit_price REAL,
    opened_at TEXT NOT NULL
);
"""


class StateManager:
    def __init__(self, stake_amount: float):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit off manually
        self.conn.execute("PRAGMA journal_mode=WAL")  # crash-safe writes
        self.conn.executescript(SCHEMA)
        self._init_row(stake_amount)

    def _init_row(self, stake_amount: float):
        cur = self.conn.execute("SELECT id FROM risk_state WHERE id = 1")
        if cur.fetchone() is None:
            self.conn.execute(
                "INSERT INTO risk_state (id, trading_balance, daily_reset_at, updated_at) "
                "VALUES (1, 0, ?, ?)",
                (self._today(), self._now()),
            )

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date().isoformat()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.snapshot()  # every state-changing transaction gets snapshotted

    def get_risk_state(self) -> dict:
        cur = self.conn.execute("SELECT * FROM risk_state WHERE id = 1")
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row))

    def update_risk_state(self, **fields):
        with self.transaction() as conn:
            fields["updated_at"] = self._now()
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE risk_state SET {set_clause} WHERE id = 1", tuple(fields.values()))

    def record_trade_open(self, client_order_id, exchange, symbol, side, amount,
                           entry_price, stop_loss_price=None, take_profit_price=None):
        with self.transaction() as conn:
            now = self._now()
            conn.execute(
                "INSERT INTO trades (client_order_id, exchange, symbol, side, amount, "
                "entry_price, status, opened_at) VALUES (?,?,?,?,?,?, 'open', ?)",
                (client_order_id, exchange, symbol, side, amount, entry_price, now),
            )
            conn.execute(
                "INSERT INTO open_positions (client_order_id, exchange, symbol, side, amount, "
                "entry_price, stop_loss_price, take_profit_price, opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (client_order_id, exchange, symbol, side, amount, entry_price,
                 stop_loss_price, take_profit_price, now),
            )

    def record_trade_close(self, client_order_id, exit_price, pnl):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE trades SET exit_price=?, status='closed', pnl=?, closed_at=? "
                "WHERE client_order_id=?",
                (exit_price, pnl, self._now(), client_order_id),
            )
            conn.execute("DELETE FROM open_positions WHERE client_order_id=?", (client_order_id,))

    def get_open_positions(self):
        cur = self.conn.execute("SELECT * FROM open_positions")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def is_duplicate_order(self, client_order_id) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM trades WHERE client_order_id = ?", (client_order_id,)
        )
        return cur.fetchone() is not None

    def snapshot(self):
        """Human-readable JSON dump — read by dashboard and the local watchdog."""
        state = self.get_risk_state()
        state["open_positions"] = self.get_open_positions()
        SNAPSHOT_PATH.write_text(json.dumps(state, indent=2, default=str))

    def backup_now(self) -> Path:
        """Hot-copy the SQLite DB (safe even while WAL is active) into data/backups/."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = BACKUP_DIR / f"state_{ts}.db"
        with sqlite3.connect(dest) as dest_conn:
            self.conn.backup(dest_conn)
        return dest
