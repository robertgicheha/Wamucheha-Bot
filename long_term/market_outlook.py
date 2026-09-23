"""
Daily outlook for the forex / crypto / gold instruments the bot trades.

Built from free yfinance daily bars (one batched download), independent of
the trading engine, so the dashboard and digest have it even when the engine
is down. Per instrument: price, 1d/1w/1m/3m change, RSI(14), 50/200-day
trend, 30-day annualised volatility, and a Bullish / Bearish / Neutral read
with the reasons. Trend context only — not a prediction.
"""
import logging
import math
from datetime import datetime, timezone

logger = logging.getLogger("market_outlook")

# MT5 / OANDA symbol -> (display name, Yahoo ticker, asset class)
_SPECIAL = {
    "XAU": ("XAU/USD (Gold)", "GC=F", "gold"),
    "XAG": ("XAG/USD (Silver)", "SI=F", "commodity"),
}


def _instrument(symbol: str):
    s = symbol.upper().replace("_", "/")
    if s[:3] in _SPECIAL:
        return _SPECIAL[s[:3]]
    if "/" not in s and len(s) == 6:          # MT5 style: EURUSD, BTCUSD
        s = f"{s[:3]}/{s[3:]}"
    base, quote = s.split("/")[:2]
    if quote in ("USDT", "USDC") or base in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA"):
        return f"{base}/USD", f"{base}-USD", "crypto"
    return f"{base}/{quote}", f"{base}{quote}=X", "forex"


def traded_instruments(config: dict) -> dict:
    """{yahoo_ticker: {name, class, brokers}} for everything the bot trades, plus gold."""
    exe = config.get("execution", {})
    pairs = []
    for ex in exe.get("exchanges", []):
        if ex.get("enabled") and ex["name"] not in ("oanda", "alpaca", "mt5"):
            pairs += [(ex["name"], m) for m in ex.get("markets", [])]
    pairs += [("oanda", m) for m in exe.get("oanda_markets", [])]
    pairs += [("mt5", m) for m in exe.get("mt5_markets", [])]
    pairs.append(("reference", "XAUUSD"))

    out = {}
    for broker, symbol in pairs:
        try:
            name, yahoo, cls = _instrument(symbol)
        except ValueError:
            continue
        entry = out.setdefault(yahoo, {"name": name, "class": cls, "brokers": []})
        if broker != "reference" and broker not in entry["brokers"]:
            entry["brokers"].append(broker)
    return out


def _rsi(closes, period: int = 14):
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1]) if len(rsi) and not math.isnan(rsi.iloc[-1]) else None


def _pct(closes, bars: int):
    if len(closes) <= bars:
        return None
    return round((closes.iloc[-1] / closes.iloc[-1 - bars] - 1) * 100, 2)


def analyze_instrument(name: str, cls: str, closes) -> dict:
    closes = closes.dropna()
    price = float(closes.iloc[-1])
    week, month, quarter = (7, 30, 90) if cls == "crypto" else (5, 21, 63)
    sma50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
    sma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
    rsi = _rsi(closes)
    periods = 365 if cls == "crypto" else 252
    vol = float(closes.pct_change().tail(30).std() * math.sqrt(periods) * 100) if len(closes) > 30 else None

    notes, bull, bear = [], 0, 0
    if sma50 and sma200:
        if price > sma50 > sma200:
            trend, bull = "Uptrend", bull + 2
            notes.append("Price above rising 50 & 200-day averages")
        elif price < sma50 < sma200:
            trend, bear = "Downtrend", bear + 2
            notes.append("Price below falling 50 & 200-day averages")
        else:
            trend = "Sideways"
            notes.append("Mixed moving averages — no clear trend")
    elif sma50:
        if price > sma50:
            trend, bull = "Uptrend", bull + 1
        else:
            trend, bear = "Downtrend", bear + 1
    else:
        trend = "Unknown"

    m1 = _pct(closes, month)
    if m1 is not None:
        if m1 > 2:
            bull += 1
        elif m1 < -2:
            bear += 1
    if rsi is not None:
        if rsi >= 70:
            bear += 1
            notes.append(f"RSI {rsi:.0f} — overbought, pullback risk")
        elif rsi <= 30:
            bull += 1
            notes.append(f"RSI {rsi:.0f} — oversold, bounce potential")

    outlook = "Bullish" if bull - bear >= 2 else "Bearish" if bear - bull >= 2 else "Neutral"
    return {
        "name": name, "class": cls, "price": round(price, 5 if price < 10 else 2),
        "change_1d": _pct(closes, 1), "change_1w": _pct(closes, week),
        "change_1m": m1, "change_3m": _pct(closes, quarter),
        "rsi": round(rsi, 1) if rsi is not None else None,
        "sma50": sma50, "sma200": sma200,
        "volatility_pct": round(vol, 1) if vol is not None else None,
        "trend": trend, "outlook": outlook, "notes": notes,
    }


def build_market_outlook(config: dict) -> dict:
    instruments = traded_instruments(config)
    result = {"instruments": [], "updated_at": datetime.now(timezone.utc).isoformat()}
    if not instruments:
        return result
    try:
        import yfinance as yf
        tickers = list(instruments)
        data = yf.download(tickers, period="1y", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        logger.warning(f"Market outlook download failed: {e}")
        return result

    for yahoo, meta in instruments.items():
        try:
            closes = data[yahoo]["Close"] if len(instruments) > 1 else data["Close"]
            if closes.dropna().empty:
                continue
            row = analyze_instrument(meta["name"], meta["class"], closes)
            row.update({"symbol": yahoo, "brokers": meta["brokers"]})
            result["instruments"].append(row)
        except Exception as e:
            logger.warning(f"Market outlook failed for {yahoo}: {e}")

    order = {"gold": 0, "commodity": 1, "forex": 2, "crypto": 3}
    result["instruments"].sort(key=lambda r: (order.get(r["class"], 9), r["name"]))
    return result
