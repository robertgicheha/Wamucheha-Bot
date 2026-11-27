"""
US stocks and ETFs from Yahoo Finance (yfinance, no API key; unofficial and may be delayed).

Sokowatch-Web has no US feed. yfinance is already this bot's market-data fallback.
Indicators use the dividend-adjusted close; the price shown is the quoted close.
"""
import re
import threading
import time
from datetime import date, timedelta

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
CACHE_TTL = 900
RETRY_SECONDS = 120
# All-time figures need the long record. Yahoo's "max" asks back to the 1920s for old companies and
# those requests fail often, so history starts here instead.
HISTORY_START = "1970-01-02"
LIVE_TTL = 60           # latest bars are refreshed this often between full downloads
GROWTH_YEARS = 6        # dividend growth compares at most this many complete years

_lock = threading.Lock()
_cache: dict = {}      # symbol -> (fetched unix time, series)
_failed: dict = {}     # symbol -> unix time before which it is not retried
_live: dict = {}       # symbol -> unix time its latest bars were last refreshed
_intraday: dict = {}   # (symbol, range) -> (unix time, points)


def _download(symbols: list, period: str = "") -> dict:
    """{symbol: {"dates", "open", "high", "low", "close", "adj", "volume", "dividends": [(date, amount)]}}."""
    import yfinance as yf
    yahoo = {s: s.replace(".", "-") for s in symbols}        # BRK.B is BRK-B on Yahoo
    span = {"period": period} if period else {"start": HISTORY_START}
    df = yf.download(list(yahoo.values()), interval="1d", auto_adjust=False, actions=True,
                     group_by="ticker", progress=False, threads=True, timeout=30, **span)
    out = {}
    if df is None or df.empty:
        return out
    for symbol, ticker in yahoo.items():
        try:
            frame = df[ticker] if df.columns.nlevels > 1 else df
            frame = frame[frame["Close"].notna() & (frame["Close"] > 0)]
        except KeyError:
            continue
        if frame.empty:
            continue
        days = [d.date().isoformat() for d in frame.index]
        adjusted = frame["Adj Close"] if "Adj Close" in frame else frame["Close"]
        paid = frame["Dividends"].fillna(0) if "Dividends" in frame else [0] * len(days)
        prices = {k: [float(v) for v in (frame[k.title()].fillna(frame["Close"]) if k.title() in frame else frame["Close"])]
                  for k in ("open", "high", "low")}
        out[symbol] = {"dates": days, **prices, "close": [float(v) for v in frame["Close"]],
                       "adj": [float(v) for v in adjusted.fillna(frame["Close"])],
                       "volume": [float(v) for v in frame["Volume"].fillna(0)],
                       "dividends": [(d, float(v)) for d, v in zip(days, paid) if v > 0]}
    return out


def get_series(symbols: list) -> tuple:
    """(series by symbol, symbols served from an expired cache, error text)."""
    error = ""
    with _lock:
        now = time.time()
        due = [s for s in symbols
               if (s not in _cache or now - _cache[s][0] >= CACHE_TTL) and now >= _failed.get(s, 0)]
        if due:
            try:
                got = _download(due)
            except Exception as e:
                got, error = {}, str(e)[:200]
            missed = [s for s in due if s not in got]
            if got and missed:      # a few tickers dropping out of a large batch is usually temporary
                try:
                    got.update(_download(missed))
                except Exception as e:
                    error = str(e)[:200]
            for s in due:
                if s in got:
                    _cache[s] = (now, got[s])
                    _live[s] = now
                    _failed.pop(s, None)
                else:
                    _failed[s] = now + RETRY_SECONDS
        found = {s: _cache[s][1] for s in symbols if s in _cache}
        stale = [s for s in found if now - _cache[s][0] >= CACHE_TTL]
    return found, stale, error


_BAR_KEYS = ("open", "high", "low", "close", "adj", "volume")


def merge_bars(series: dict, recent: dict) -> dict:
    """A copy of `series` with its last days replaced or extended by `recent`."""
    if not recent["dates"]:
        return series
    keep = sum(d < recent["dates"][0] for d in series["dates"])
    out = {k: series[k][:keep] + recent[k] for k in ("dates", *_BAR_KEYS)}
    out["dividends"] = sorted({**dict(series["dividends"]), **dict(recent["dividends"])}.items())
    return out


def refresh_latest(symbols: list) -> None:
    """Brings cached series up to the current price without re-downloading their history."""
    with _lock:
        now = time.time()
        due = [s for s in symbols if s in _cache and now - _live.get(s, 0) >= LIVE_TTL]
        if not due:
            return
        for s in due:
            _live[s] = now
        try:
            got = _download(due, "5d")
        except Exception as e:
            print(f"[stocks] live refresh failed: {e}")
            return
        for s, recent in got.items():
            # A split rewrites history: if the overlap no longer lines up, download it all again.
            old = dict(zip(_cache[s][1]["dates"][-10:], _cache[s][1]["close"][-10:]))
            before = old.get(recent["dates"][0])
            if before and abs(recent["close"][0] / before - 1) > 0.3:
                _cache[s] = (0, _cache[s][1])
                continue
            _cache[s] = (_cache[s][0], merge_bars(_cache[s][1], recent))


def _fetch_intraday(symbol: str, days: str) -> list:
    import yfinance as yf
    frame = yf.Ticker(symbol.replace(".", "-")).history(period=days, interval="1m" if days == "1d" else "5m")
    if frame is None or frame.empty:
        return []
    points = []
    for stamp, value, volume in zip(frame.index, frame["Close"], frame["Volume"]):
        if value == value:
            # The chart draws UTC, so shift by the exchange offset to show New York time.
            local = int(stamp.timestamp() + stamp.utcoffset().total_seconds())
            points.append({"time": local, "value": round(float(value), 4), "volume": float(volume or 0)})
    return points


def intraday(symbol: str, days: str = "1d") -> list:
    hit = _intraday.get((symbol, days))
    if hit and time.time() - hit[0] < LIVE_TTL:
        return hit[1]
    points = _fetch_intraday(symbol, days)
    _intraday[(symbol, days)] = (time.time(), points)
    return points


def dividend_summary(series: dict, today: date | None = None) -> dict | None:
    """Trailing 12-month yield and the payment record, or None if nothing was ever paid."""
    paid = series.get("dividends") or []
    if not paid:
        return None
    today = today or date.today()
    cutoff = (today - timedelta(days=365)).isoformat()
    recent = [amount for day, amount in paid if day > cutoff]
    yearly: dict = {}
    for day, amount in paid:
        yearly[int(day[:4])] = yearly.get(int(day[:4]), 0) + amount
    # Compare complete calendar years only: the first and current years are partial.
    full = [y for y in sorted(yearly) if int(series["dates"][0][:4]) < y < today.year][-GROWTH_YEARS:]
    growth = None
    if len(full) >= 2 and yearly[full[0]] > 0:
        growth = round(((yearly[full[-1]] / yearly[full[0]]) ** (1 / (full[-1] - full[0])) - 1) * 100, 1)
    return {"annual_per_share": round(sum(recent), 4),
            "yield_pct": round(sum(recent) / series["close"][-1] * 100, 2),
            "payments_last_year": len(recent), "last_ex_date": paid[-1][0],
            "last_amount": round(paid[-1][1], 4), "annual_growth_pct": growth,
            "by_year": [{"year": y, "total": round(yearly[y], 4), "complete": y in full} for y in sorted(yearly)],
            "payments": [{"date": d, "amount": round(a, 4)} for d, a in paid[-12:]][::-1],
            "source": "Yahoo Finance"}
