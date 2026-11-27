"""
Screener columns worked out from daily prices: performance windows, relative volume,
highs and lows, a technical rating and monthly seasonals.

A "series" is {"dates", "close", "volume"} plus optional "open", "high", "low".
Performance uses the quoted (split-adjusted) price, dividends not included.
"""
from datetime import date, timedelta

from stock_analysis.analytics import rsi, volatility

# label -> calendar days back
WINDOWS = {"1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365, "5y": 1826}
MIN_52W_BARS = 240


def _pct(now: float, then: float):
    return round((now / then - 1) * 100, 2) if then else None


def performance(dates: list, close: list) -> dict:
    """Change against the last close on or before each window's start. None when history
    does not reach back that far, so a short record never poses as a long return."""
    out = {key: None for key in (*WINDOWS, "ytd", "all")}
    if len(close) < 2:
        return out
    last_day = date.fromisoformat(dates[-1])

    def base(cutoff: str):
        for i in range(len(dates) - 1, -1, -1):
            if dates[i] <= cutoff:
                return close[i]
        return None

    for key, days in WINDOWS.items():
        out[key] = _pct(close[-1], base((last_day - timedelta(days=days)).isoformat()))
    out["ytd"] = _pct(close[-1], base(f"{last_day.year - 1}-12-31"))
    out["all"] = _pct(close[-1], close[0])
    return out


def ema_series(values: list, n: int) -> list:
    """EMA seeded with the simple average of the first n values."""
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _signal(buy: bool, sell: bool) -> str:
    return "Buy" if buy else "Sell" if sell else "Neutral"


def rating(signals: list) -> dict:
    """Net share of buy signals, in TradingView's five bands."""
    total = len(signals)
    buy = sum(s["signal"] == "Buy" for s in signals)
    sell = sum(s["signal"] == "Sell" for s in signals)
    if not total:
        return {"label": None, "score": None, "buy": 0, "sell": 0, "neutral": 0}
    score = (buy - sell) / total
    label = ("Strong buy" if score > .5 else "Buy" if score > .1 else "Neutral" if score >= -.1
             else "Sell" if score >= -.5 else "Strong sell")
    return {"label": label, "score": round(score, 3), "buy": buy, "sell": sell, "neutral": total - buy - sell}


def technicals(close: list, high: list | None = None, low: list | None = None) -> dict:
    close = close[-600:]
    price = close[-1] if close else 0
    averages = []
    for n in (10, 20, 30, 50, 100, 200):
        if len(close) >= n:
            for name, value in ((f"SMA {n}", sum(close[-n:]) / n), (f"EMA {n}", ema_series(close, n)[-1])):
                averages.append({"name": name, "value": value, "signal": _signal(price > value, price < value)})
    osc = []
    r = rsi(close) if len(close) >= 30 else None
    if r is not None:
        osc.append({"name": "RSI (14)", "value": r, "signal": _signal(r < 30, r > 70)})
    if len(close) >= 11:
        m = price - close[-11]
        if len(close) >= 30:
            osc.append({"name": "Momentum (10)", "value": m, "signal": _signal(m > 0, m < 0)})
    if len(close) >= 35:
        slow = ema_series(close, 26)
        fast = ema_series(close, 12)[-len(slow):]
        line = [f - s for f, s in zip(fast, slow)]
        trigger = ema_series(line, 9)[-1]
        osc.append({"name": "MACD (12, 26)", "value": line[-1],
                    "signal": _signal(line[-1] > trigger, line[-1] < trigger)})
    if high and low and len(close) >= 30:
        high, low = high[-len(close):], low[-len(close):]
        top, bottom = max(high[-14:]), min(low[-14:])
        if top > bottom:
            k = (price - bottom) / (top - bottom) * 100
            osc.append({"name": "Stochastic %K (14)", "value": k, "signal": _signal(k < 20, k > 80)})
        typical = [(h + lo + c) / 3 for h, lo, c in zip(high[-20:], low[-20:], close[-20:])]
        mean = sum(typical) / 20
        spread = sum(abs(t - mean) for t in typical) / 20
        if spread:
            cci = (typical[-1] - mean) / (0.015 * spread)
            osc.append({"name": "CCI (20)", "value": cci, "signal": _signal(cci < -100, cci > 100)})
    for item in (*averages, *osc):
        item["value"] = round(item["value"], 4)
    return {"moving_averages": averages, "oscillators": osc, "ma_rating": rating(averages),
            "osc_rating": rating(osc), "summary": rating(averages + osc)}


def row_metrics(series: dict | None, price: float, volume: float) -> dict:
    """Everything the screener table and filters need from one price series."""
    s = series or {"dates": [], "close": [], "volume": []}
    close, vols = s["close"], s["volume"]
    highs, lows = s.get("high") or close, s.get("low") or close
    out = {"perf": performance(s["dates"], close), "bars": len(close), "rel_vol": None,
           "high_52w": None, "low_52w": None, "rsi": rsi(close[-600:]) if len(close) >= 30 else None,
           "volatility_1m": volatility(close, 21), "spark": [round(c, 4) for c in close[-30:]]}
    if len(vols) > 10:
        usual = sum(vols[-11:-1]) / 10
        out["rel_vol"] = round(volume / usual, 2) if usual > 0 else None
    if len(close) >= MIN_52W_BARS:
        out["high_52w"], out["low_52w"] = round(max(highs[-252:]), 4), round(min(lows[-252:]), 4)
    t = technicals(close, s.get("high"), s.get("low"))
    out["tech"], out["tech_ma"], out["tech_osc"] = (t["summary"]["label"], t["ma_rating"]["label"],
                                                    t["osc_rating"]["label"])
    return out


def seasonals(dates: list, close: list, years: int = 6) -> list:
    """Month-by-month price change for recent years, newest first."""
    month_end: dict = {}
    for day, value in zip(dates, close):
        month_end[(int(day[:4]), int(day[5:7]))] = value
    table: dict = {}
    previous = None
    for key in sorted(month_end):
        year, month = key
        follows = previous and (year * 12 + month) - (previous[0] * 12 + previous[1]) == 1
        if follows:
            table.setdefault(year, [None] * 12)[month - 1] = _pct(month_end[key], month_end[previous])
        previous = key
    return [{"year": y, "months": table[y]} for y in sorted(table, reverse=True)[:years]]
