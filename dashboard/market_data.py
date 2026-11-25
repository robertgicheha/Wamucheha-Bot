"""
Candlestick data for the admin console's Market tab.

The dashboard is a separate process from the engine, so it fetches its own
read-only candles. Provider order for source="auto":
  1. the MT5 terminal (same candles you see on the MT5 chart), if reachable
  2. public exchange data through ccxt (crypto pairs such as BTC/USDT), no API key
  3. yfinance (gold / forex / crypto fallback)
Every provider is wrapped so one failing source never breaks the page, and a
short cache keeps auto-refresh from hammering any of them.
"""
import concurrent.futures
import os
import threading
import time

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
# okx first: it is the venue in config.yaml and works from most networks. PRICE_EXCHANGES overrides.
CCXT_EXCHANGES = [e.strip() for e in os.environ.get("PRICE_EXCHANGES", "okx,binance,bybit").split(",") if e.strip()]
_ccxt_down_until: dict = {}   # exchange -> unix time before which it is skipped (it just failed)
DEFAULT_CRYPTO = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

_cache: dict = {}
_slow_cache: dict = {}
_mt5_lock = threading.Lock()   # the MT5 API is not safe to drive from several threads at once
CACHE_TTL = 8
_mt5_down_until = 0.0
_ccxt_clients: dict = {}


def default_symbols(configured_mt5: list | None = None) -> list:
    seen, out = set(), []
    for s in list(configured_mt5 or []) + DEFAULT_CRYPTO + ["EURUSD", "GBPUSD"]:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _rows(df) -> list:
    """DataFrame (index = time or column 'time') -> list of candle dicts, UTC seconds."""
    out = []
    for ts, r in df.iterrows():
        t = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        if any(v != v for v in (o, h, l, c)):          # NaN
            continue
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c,
                    "volume": float(r.get("volume", 0) or 0)})
    return out


def _from_mt5(symbol: str, tf: str, limit: int) -> list:
    global _mt5_down_until
    if time.time() < _mt5_down_until:
        raise RuntimeError("MT5 unavailable (retrying shortly)")
    if not os.environ.get("MT5_LOGIN"):
        raise RuntimeError("MT5 not configured")

    def work():
        from core.mt5_client import get_mt5
        from data_feeds.mt5_feed import TIMEFRAME_MAP
        mt5 = get_mt5()
        kw = {"timeout": 5000}
        if os.environ.get("MT5_TERMINAL_PATH"):
            kw["path"] = os.environ["MT5_TERMINAL_PATH"]
        if not mt5.initialize(**kw):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, TIMEFRAME_MAP[tf]), 0, limit)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 has no {symbol} data")
        return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r["tick_volume"])} for r in rates]

    with _mt5_lock, concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(work)
        try:
            return fut.result(timeout=8)
        except Exception:
            _mt5_down_until = time.time() + 120      # do not hammer a dead terminal
            raise


def _from_ccxt(symbol: str, tf: str, limit: int) -> tuple:
    import ccxt
    last = None
    for name in CCXT_EXCHANGES:
        if time.time() < _ccxt_down_until.get(name, 0):
            continue                                   # failed recently: do not wait on it again
        try:
            ex = _ccxt_clients.get(name) or getattr(ccxt, name)({"enableRateLimit": True, "timeout": 5000})
            _ccxt_clients[name] = ex
            raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
            if raw:
                return [{"time": int(r[0] // 1000), "open": r[1], "high": r[2], "low": r[3],
                         "close": r[4], "volume": r[5] or 0} for r in raw], name
        except ccxt.BadSymbol as e:                    # this exchange just does not list it: not an outage
            last = e
        except Exception as e:                         # geo-block, timeout, DNS: back off this exchange
            last = e
            _ccxt_down_until[name] = time.time() + 300
    raise RuntimeError(f"no exchange returned {symbol}: {last}")


_YF_MAP = {"XAUUSD": "GC=F", "XAGUSD": "SI=F", "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
           "SOLUSD": "SOL-USD", "XRPUSD": "XRP-USD", "LTCUSD": "LTC-USD"}


def _from_yfinance(symbol: str, tf: str, limit: int) -> list:
    import yfinance as yf
    # Brokers append account-type suffixes (Exness: XAUUSDm, BTCUSDc). Strip one for lookup.
    if "/" not in symbol and len(symbol) > 6 and symbol[-1] in "mcMC" and symbol[:-1].isalpha() and symbol[:-1].isupper():
        symbol = symbol[:-1]
    if "/" in symbol:
        base, quote = symbol.split("/")
        ysym = f"{base}-{'USD' if quote in ('USDT', 'USDC') else quote}"
    elif symbol in _YF_MAP:
        ysym = _YF_MAP[symbol]
    elif len(symbol) == 6 and symbol.isalpha():
        ysym = symbol + "=X"
    else:
        ysym = symbol
    interval = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "4h": "60m", "1d": "1d"}[tf]
    period = {"1m": "5d", "5m": "30d", "15m": "30d", "30m": "30d", "1h": "180d", "4h": "360d", "1d": "2y"}[tf]
    df = yf.Ticker(ysym).history(period=period, interval=interval)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance has no data for {ysym}")
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    if tf == "4h":
        df = df.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                                    "close": "last", "volume": "sum"}).dropna()
    return _rows(df.tail(limit))


def fetch_candles(symbol: str, tf: str = "15m", limit: int = 300, source: str = "auto") -> dict:
    if tf not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
    limit = max(20, min(int(limit), 1000))
    key = (symbol, tf, limit, source)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    order = {"auto": ["mt5", "ccxt", "yfinance"], "mt5": ["mt5"], "exchange": ["ccxt"],
             "yfinance": ["yfinance"]}.get(source, ["mt5", "ccxt", "yfinance"])
    if "/" not in symbol and source == "auto":
        order = [p for p in order if p != "ccxt"]    # exchange pairs only make sense with BASE/QUOTE
    errors, candles, used = [], None, None
    for prov in order:
        try:
            if prov == "mt5":
                candles, used = _from_mt5(symbol, tf, limit), "MT5 terminal"
            elif prov == "ccxt":
                candles, ex = _from_ccxt(symbol, tf, limit)
                used = f"{ex} (public)"
            else:
                candles, used = _from_yfinance(symbol, tf, limit), "yfinance"
            break
        except Exception as e:
            errors.append(f"{prov}: {e}")
    if not candles:
        raise RuntimeError("; ".join(errors) or "no data")
    candles.sort(key=lambda c: c["time"])
    result = {"symbol": symbol, "timeframe": tf, "source": used, "candles": candles,
              "last_price": candles[-1]["close"], "fallback_notes": errors}
    _cache[key] = (time.time(), result)
    return result


def _change_24h(symbol: str):
    """% change versus ~24h ago, from hourly candles (cached for 2 minutes: it moves slowly)."""
    hit = _slow_cache.get(symbol)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    val = None
    try:
        cs = fetch_candles(symbol, "1h", 30)["candles"]
        ref = cs[-24]["close"] if len(cs) >= 24 else cs[0]["close"]
        if ref:
            val = round((cs[-1]["close"] - ref) / ref * 100, 3)
    except Exception:
        pass
    _slow_cache[symbol] = (time.time(), val)
    return val


def snapshot(symbol: str) -> dict:
    """Latest price + 24h change for one symbol. Never raises: a dead feed becomes an error row."""
    try:
        c = fetch_candles(symbol, "1m", 30)
        return {"symbol": symbol, "price": c["last_price"], "source": c["source"],
                "change_pct": _change_24h(symbol), "ts": c["candles"][-1]["time"]}
    except Exception as e:
        return {"symbol": symbol, "price": None, "error": str(e)[:160]}


_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="snap")


def snapshots(symbols: list, timeout: float = 6.0) -> list:
    """Snapshots for several symbols in parallel. A slow feed must not stall the page: after
    `timeout` seconds it is reported as loading, keeps running in the background, and its
    result is served from cache on the next refresh."""
    if not symbols:
        return []
    futs = {s: _pool.submit(snapshot, s) for s in symbols}
    concurrent.futures.wait(list(futs.values()), timeout=timeout)
    out = []
    for s, f in futs.items():
        out.append(f.result() if f.done() else {"symbol": s, "price": None, "error": "loading..."})
    return out
