"""
Indicators, historical rise frequency and the ranked pick lists.

Everything is computed from daily closes with fixed, visible rules. The "outlook" is
not a model's forecast: it counts how often this security was higher 21 trading days
later on past days that had the same trend set-up as today, next to how often it was
higher on all days. The gap between the two is the only thing the set-up adds.
"""
import math

MIN_BARS = 60             # below this there is nothing worth computing
HORIZON = 21              # trading days, about one month
MIN_INDEPENDENT = 6       # forward windows overlap, so matches/21 is the honest sample size
RISE_MIN_PCT = 55.0       # "likely to rise" needs at least this historical frequency
BEST_MIN_SCORE = 65
SPECULATIVE_VOL = 40.0    # annualised %, above this a name is treated as speculative

DISCLAIMER = ("Educational screening from delayed prices and fixed rules. It is not financial advice and "
              "does not predict future returns. Past frequency is not future probability. Check the company "
              "yourself or speak to a licensed adviser before investing.")


def _ret(closes: list, bars: int):
    return round((closes[-1] / closes[-1 - bars] - 1) * 100, 2) if len(closes) > bars else None


def sma(closes: list, n: int):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def rsi(closes: list, n: int = 14):
    """Wilder's RSI."""
    if len(closes) <= n:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(d, 0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0) for d in deltas[:n]) / n
    for d in deltas[n:]:
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
    if loss == 0:
        return 100.0 if gain else 50.0
    return round(100 - 100 / (1 + gain / loss), 1)


def volatility(closes: list, window: int = 63):
    """Annualised standard deviation of daily returns, in percent."""
    tail = closes[-(window + 1):]
    if len(tail) < 21:
        return None
    r = [b / a - 1 for a, b in zip(tail, tail[1:])]
    mean = sum(r) / len(r)
    return round(math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1) * 252) * 100, 1)


def max_drawdown(closes: list):
    peak, worst = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, c / peak - 1)
    return round(worst * 100, 1)


def outlook(closes: list):
    """Historical frequency of a higher price HORIZON days later, or None without enough data."""
    n = len(closes)
    if n < 200 + HORIZON * MIN_INDEPENDENT:
        return None
    prefix = [0.0]
    for c in closes:
        prefix.append(prefix[-1] + c)

    def state(i):
        fast = (prefix[i + 1] - prefix[i - 49]) / 50
        slow = (prefix[i + 1] - prefix[i - 199]) / 200
        return closes[i] > fast, fast > slow, closes[i] > closes[i - HORIZON]

    today = state(n - 1)
    every, matched = [], []
    for i in range(199, n - HORIZON):                # only days whose outcome is already known
        forward = closes[i + HORIZON] / closes[i] - 1
        every.append(forward)
        if state(i) == today:
            matched.append(forward)
    if not matched:
        return None
    hit = sum(f > 0 for f in matched) / len(matched) * 100
    base = sum(f > 0 for f in every) / len(every) * 100
    ordered = sorted(matched)
    independent = len(matched) // HORIZON
    return {"horizon_days": HORIZON, "rose_pct": round(hit, 1), "all_days_rose_pct": round(base, 1),
            "edge_pct": round(hit - base, 1), "median_move_pct": round(ordered[len(ordered) // 2] * 100, 2),
            "matches": len(matched), "independent_samples": independent,
            "reliable": independent >= MIN_INDEPENDENT,
            "setup": {"above_50d_average": today[0], "50d_above_200d": today[1], "up_over_last_month": today[2]}}


def analyze(closes: list, volumes: list | None = None) -> dict:
    """All indicators for one security. Keys that need more history are None."""
    out = {"bars": len(closes), "enough_history": len(closes) >= MIN_BARS}
    if not out["enough_history"]:
        return out
    year = closes[-252:]
    out.update({
        "ret_1w": _ret(closes, 5), "ret_1m": _ret(closes, 21), "ret_3m": _ret(closes, 63),
        "ret_6m": _ret(closes, 126), "ret_1y": _ret(closes, 252),
        "sma50": sma(closes, 50), "sma200": sma(closes, 200), "rsi14": rsi(closes),
        "volatility_pct": volatility(closes), "max_drawdown_1y_pct": max_drawdown(year),
        "from_high_pct": round((closes[-1] / max(year) - 1) * 100, 1),
        "high_window_days": len(year),
        "volume_ratio": None, "outlook": outlook(closes),
    })
    if volumes and len(volumes) > 21:
        usual = sum(volumes[-21:-1]) / 20
        out["volume_ratio"] = round(volumes[-1] / usual, 2) if usual > 0 else None
    out.update(buy_score(closes[-1], out))
    return out


def buy_score(price: float, a: dict) -> dict:
    """0-100 from trend, momentum, risk and timing. Starts at a neutral 50."""
    score, reasons = 50.0, []
    if a.get("sma50"):
        above = price > a["sma50"]
        score += 10 if above else -10
        reasons.append(f"Price is {'above' if above else 'below'} its 50-day average")
    if a.get("sma200"):
        up = a["sma50"] > a["sma200"]
        score += 10 if up else -10
        reasons.append("Long-term trend is up (50-day above 200-day)" if up else "Long-term trend is down (50-day below 200-day)")
    for key, label, points, full in (("ret_3m", "3 months", 10, 20), ("ret_6m", "6 months", 10, 30)):
        v = a.get(key)
        if v is not None:
            score += max(-1, min(1, v / full)) * points
            reasons.append(f"{v:+.1f}% over {label}")
    vol = a.get("volatility_pct")
    if vol is not None:
        if vol > SPECULATIVE_VOL:
            score -= 10
            reasons.append(f"Very volatile ({vol:.0f}% a year)")
        elif vol < 20:
            score += 5
            reasons.append(f"Relatively calm ({vol:.0f}% a year)")
    r = a.get("rsi14")
    if r is not None and r >= 70:
        score -= 10
        reasons.append(f"Overbought (RSI {r:.0f}): a pullback is a risk")
    elif r is not None and r <= 30:
        reasons.append(f"Oversold (RSI {r:.0f})")
    return {"buy_score": int(round(max(0, min(100, score)))), "buy_reasons": reasons}


def speculative_reasons(row: dict) -> list:
    """Why a high-risk trader might look at this name. Empty list: not a speculative candidate."""
    a = row.get("analysis") or {}
    if not a.get("enough_history"):
        # Recorded history is still short: only today's move can be judged.
        if abs(row.get("change_pct") or 0) >= 4 and row.get("volume", 0) >= 10_000:
            return [f"Moved {row['change_pct']:+.2f}% today on {int(row['volume']):,} shares (one day of data only)"]
        return []
    vol = a.get("volatility_pct")
    if vol is None or vol < SPECULATIVE_VOL:
        return []
    why = []
    if (a.get("ret_1m") or 0) >= 10:
        why.append(f"Strong run: {a['ret_1m']:+.1f}% in a month")
    if a.get("rsi14") is not None and a["rsi14"] <= 35:
        why.append(f"Oversold bounce candidate (RSI {a['rsi14']:.0f})")
    if (a.get("volume_ratio") or 0) >= 2:
        why.append(f"Volume is {a['volume_ratio']:.1f}x its 20-day average")
    return why + [f"Volatility {vol:.0f}% a year: large losses are possible"] if why else []


def picks(rows: list) -> dict:
    """Ranked symbol lists built from rows that already carry "analysis"."""
    scored = [r for r in rows if (r.get("analysis") or {}).get("buy_score") is not None]
    scored.sort(key=lambda r: (-r["analysis"]["buy_score"], r["symbol"]))
    # Best buys must score well, not be overbought and not be a speculative-grade swinger.
    best = [r for r in scored
            if r["analysis"]["buy_score"] >= BEST_MIN_SCORE
            and (r["analysis"].get("volatility_pct") or 0) <= SPECULATIVE_VOL
            and (r["analysis"].get("rsi14") or 0) < 70]

    rising = []
    for r in rows:
        o = (r.get("analysis") or {}).get("outlook")
        if o and o["reliable"] and o["rose_pct"] >= RISE_MIN_PCT and o["edge_pct"] > 0 and o["setup"]["above_50d_average"]:
            rising.append(r)
    rising.sort(key=lambda r: (-r["analysis"]["outlook"]["edge_pct"], -r["analysis"]["outlook"]["rose_pct"], r["symbol"]))

    spec = []
    for r in rows:
        why = speculative_reasons(r)
        if why:
            spec.append({"symbol": r["symbol"], "reasons": why})
    by_symbol = {r["symbol"]: r for r in rows}
    spec.sort(key=lambda s: -abs((by_symbol[s["symbol"]].get("analysis") or {}).get("ret_1m")
                                 or by_symbol[s["symbol"]].get("change_pct") or 0))

    payers = sorted((r for r in rows if (r.get("dividend") or {}).get("yield_pct")),
                    key=lambda r: -r["dividend"]["yield_pct"])
    return {"best_buy": [r["symbol"] for r in best[:10]],
            "likely_to_rise": [r["symbol"] for r in rising[:10]],
            "speculative": spec[:10],
            "dividends": [r["symbol"] for r in payers]}
