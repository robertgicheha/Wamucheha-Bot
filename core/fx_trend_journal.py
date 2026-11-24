"""
SQLite journal for the FX trend bot. The bot writes; the dashboard reads.

WAL mode lets the dashboard read while the bot writes. Each call opens a short
connection, so it is safe across the bot process, dashboard process and threads.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY, ts TEXT, bar TEXT, equity REAL, daily_pct REAL,
  weekly_pct REAL, halted INTEGER, n_positions INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY, ts TEXT, bar TEXT, pair TEXT, side INTEGER, close REAL,
  sma REAL, mom REAL, atr REAL, strength REAL, up_level REAL, dn_level REAL,
  p_flip_5d REAL, p_flip_20d REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, level TEXT, kind TEXT, pair TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS trades(
  ticket INTEGER PRIMARY KEY, pair TEXT, side INTEGER, volume REAL, open_time TEXT,
  close_time TEXT, entry REAL, exit REAL, profit REAL, commission REAL, swap REAL);
CREATE INDEX IF NOT EXISTS ix_signals_bar ON signals(bar, pair);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_trades_close ON trades(close_time);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- writes ----------------------------------------------------------
    def add_cycle(self, bar, equity, daily_pct, weekly_pct, halted, n_positions, status="ok"):
        with self._conn() as c:
            c.execute("INSERT INTO cycles(ts,bar,equity,daily_pct,weekly_pct,halted,n_positions,status)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (_now(), bar, equity, daily_pct, weekly_pct, int(halted), n_positions, status))

    def add_signals(self, bar: str, rows: list[dict]):
        ts = _now()
        with self._conn() as c:
            c.execute("DELETE FROM signals WHERE bar=?", (bar,))  # re-run of the same bar replaces
            c.executemany(
                "INSERT INTO signals(ts,bar,pair,side,close,sma,mom,atr,strength,up_level,dn_level,"
                "p_flip_5d,p_flip_20d) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ts, bar, r["pair"], r["side"], r["close"], r["sma"], r["mom"], r["atr"],
                  r["strength"], r["up_level"], r["dn_level"], r["p_flip_5d"], r["p_flip_20d"])
                 for r in rows])

    def add_event(self, kind: str, message: str, pair: str = "", level: str = "INFO"):
        with self._conn() as c:
            c.execute("INSERT INTO events(ts,level,kind,pair,message) VALUES(?,?,?,?,?)",
                      (_now(), level, kind, pair, message))

    def upsert_trades(self, trades: list[dict]):
        if not trades:
            return
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO trades(ticket,pair,side,volume,open_time,close_time,entry,exit,"
                "profit,commission,swap) VALUES(:ticket,:pair,:side,:volume,:open_time,:close_time,"
                ":entry,:exit,:profit,:commission,:swap)", trades)

    # ---- reads -----------------------------------------------------------
    def _rows(self, sql, args=()):
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def equity_series(self, limit: int = 2000) -> list[dict]:
        rows = self._rows("SELECT ts,bar,equity,daily_pct,weekly_pct,halted,n_positions FROM cycles "
                          "ORDER BY id DESC LIMIT ?", (limit,))
        return rows[::-1]

    def events(self, limit: int = 200, level: str | None = None) -> list[dict]:
        if level:
            return self._rows("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit))
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def trades(self, limit: int = 1000) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY close_time DESC LIMIT ?", (limit,))

    def latest_signals(self) -> list[dict]:
        return self._rows("SELECT * FROM signals WHERE bar=(SELECT MAX(bar) FROM signals) ORDER BY pair")

    def signal_history(self, pair: str, limit: int = 250) -> list[dict]:
        rows = self._rows("SELECT bar,side,close,sma,mom,atr,strength FROM signals WHERE pair=? "
                          "ORDER BY bar DESC LIMIT ?", (pair, limit))
        return rows[::-1]
