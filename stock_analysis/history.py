"""
Daily NSE closes, recorded locally.

The NSE feed has no history endpoint (Sokowatch-Web solves this the same way, with its
stock_prices table). One row per symbol per trading day; the latest fetch of the day wins.
Older history can be loaded from a CSV with date,symbol,close columns (volume optional).
Prices are stored as quoted: splits and bonus issues are not adjusted for.
"""
import csv
import sqlite3
from contextlib import closing
from datetime import date

from stock_analysis.storage import data_dir


def _path():
    return data_dir() / "nse_history.db"


def _connect():
    _path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_path(), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS closes (symbol TEXT NOT NULL, day TEXT NOT NULL, "
                 "close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, PRIMARY KEY (symbol, day))")
    return conn


def _upsert(rows: list) -> int:
    with closing(_connect()) as conn, conn:
        conn.executemany("INSERT INTO closes (symbol, day, close, volume) VALUES (?,?,?,?) "
                         "ON CONFLICT(symbol, day) DO UPDATE SET close=excluded.close, volume=excluded.volume", rows)
    return len(rows)


TICK_DAYS = 10           # intraday points kept for the 1-day and 5-day charts


def record_tick(stamp: int, stocks: list) -> None:
    """One intraday point per symbol for each price fetch."""
    with closing(_connect()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ticks (symbol TEXT NOT NULL, ts INTEGER NOT NULL, "
                     "price REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, PRIMARY KEY (symbol, ts))")
        conn.executemany("INSERT OR REPLACE INTO ticks (symbol, ts, price, volume) VALUES (?,?,?,?)",
                         [(s["symbol"], stamp, s["price"], s["volume"]) for s in stocks
                          if s["symbol"] and s["price"] > 0])
        conn.execute("DELETE FROM ticks WHERE ts < ?", (stamp - TICK_DAYS * 86400,))


def ticks(symbol: str) -> list:
    """[(unix time, price, volume)] oldest first."""
    if not _path().exists():
        return []
    try:
        with closing(sqlite3.connect(_path(), timeout=10)) as conn:
            return conn.execute("SELECT ts, price, volume FROM ticks WHERE symbol=? ORDER BY ts",
                                (symbol,)).fetchall()
    except sqlite3.Error:      # nothing has been fetched since this table was introduced
        return []


def record(day: str, stocks: list) -> int:
    return _upsert([(s["symbol"], day, s["price"], s["volume"]) for s in stocks if s["symbol"] and s["price"] > 0])


def import_csv(path: str) -> int:
    """Bad rows abort the whole import, naming the line, so history is never half-loaded."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for line, raw in enumerate(csv.DictReader(handle), start=2):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                day = date.fromisoformat(r["date"]).isoformat()
                symbol, close = r["symbol"].upper(), float(r["close"].replace(",", ""))
                volume = float((r.get("volume") or "0").replace(",", ""))
                if not symbol or not close > 0:
                    raise ValueError("empty symbol or non-positive close")
            except (KeyError, ValueError) as e:
                raise ValueError(f"line {line}: need date (YYYY-MM-DD), symbol, close ({e})") from None
            rows.append((symbol, day, close, volume))
    return _upsert(rows)


def load_all() -> dict:
    """{symbol: {"dates": [...], "close": [...], "volume": [...]}} in date order."""
    if not _path().exists():
        return {}
    try:
        with closing(sqlite3.connect(_path(), timeout=10)) as conn:
            found = conn.execute("SELECT symbol, day, close, volume FROM closes ORDER BY symbol, day").fetchall()
    except sqlite3.Error:
        return {}
    out: dict = {}
    for symbol, day, close, volume in found:
        s = out.setdefault(symbol, {"dates": [], "close": [], "volume": []})
        s["dates"].append(day)
        s["close"].append(close)
        s["volume"].append(volume)
    return out
