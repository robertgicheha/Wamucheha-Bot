"""
Price projections for NSE Kenya securities, built from the daily-close series
that data_feeds/nse_feed.py accumulates.

READ THIS BEFORE TRUSTING A NUMBER PRODUCED HERE
-----------------------------------------------
These are statistical extrapolations, not forecasts in any meaningful sense,
and the code is deliberately structured to make that hard to overlook:

  * The whole history starts on the day the first 16:00 EAT snapshot ran.
    Nothing is backfilled — the RapidAPI history endpoints are Pro-only — so
    on day one there is one data point and every security correctly reports
    "insufficient history".
  * Expected drift is SHRUNK toward zero by n/(n+K). Without this, three
    rising days would extrapolate to a triple-digit 6-month target. With it,
    a 60-day sample still cannot produce a large directional call.
  * The band widens as sqrt(horizon), so a 6-month interval is presented as
    very wide. On an illiquid mid-cap that width is not a formality — a large
    share of the distribution is unreachable at any sensible price.
  * Every payload carries "disclaimer" and the sample size that produced it.
    Consumers must not strip those.

The model itself is a plain random-walk-with-drift on log returns:
    P(h) = P(0) * exp(mu*h)
    95% band = P(0) * exp(mu*h +/- 1.96 * sigma * sqrt(h))
It deliberately uses no earnings, no valuation and no macro inputs, because
none of those are reliably available for Kenyan listings here. A random walk
is the honest null hypothesis, and beating it is not something this codebase
claims to do.

NSE remains analysis-only: nothing here is a recommendation to buy or sell.
"""
from typing import Any

import numpy as np
import pandas as pd

# Horizons in TRADING days (NSE trades Mon-Fri, ~252 days/yr).
DEFAULT_HORIZONS: list[tuple[str, int]] = [
    ("1w", 5),
    ("1m", 21),
    ("3m", 63),
    ("6m", 126),
]

# Below this many closes there is not enough data to say anything directional.
MIN_HISTORY = 20

# Drift shrinkage constant. n/(n+K): 20 obs -> half the raw drift survives,
# 60 obs -> 75%, 200 obs -> 91%.
SHRINKAGE_K = 20.0

# Two-sided normal quantiles.
Z_80 = 1.2816
Z_95 = 1.959964

TRADING_DAYS_PER_YEAR = 252

DISCLAIMER = ("Statistical extrapolation of past price movement only. Not "
              "investment advice, not a recommendation, and not a promise of "
              "any return. NSE Kenya liquidity is thin — treat wide bands as "
              "wide, not as edge.")


def _clean_closes(history: pd.DataFrame | None) -> pd.Series:
    """Pull a clean, positive, date-sorted close series out of a history frame.

    A security can print a 0.00 close (ALP in the current snapshot) when it
    has not traded; feeding that to a log-return model would produce -inf, so
    non-positive prices are dropped and gaps are left as gaps rather than
    being interpolated into fake data.
    """
    if history is None or len(history) == 0 or "close" not in history:
        return pd.Series(dtype=float)
    closes = pd.to_numeric(history["close"], errors="coerce")
    closes = closes[closes > 0].dropna()
    return closes


def summarize_trend(history: pd.DataFrame | None) -> dict[str, Any]:
    """Moving averages, momentum and realised volatility from accumulated closes.

    Moving averages are reported only once the window is actually full — a
    "20-day average" computed from 8 points is not a 20-day average, and
    long_term/screener.py already distinguishes full from degraded mode, so
    this keeps the same convention."""
    closes = _clean_closes(history)
    n = len(closes)
    if n == 0:
        return {"observations": 0, "sma20": None, "sma50": None, "sma200": None,
                "above_sma50": None, "above_sma200": None, "momentum_30d_pct": None,
                "annualized_vol_pct": None, "last_close": None}

    last = float(closes.iloc[-1])

    def sma(window: int) -> float | None:
        return float(closes.tail(window).mean()) if n >= window else None

    def momentum(window: int) -> float | None:
        if n < window + 1:
            return None
        past = float(closes.iloc[-1 - window])
        return (last / past - 1) * 100 if past else None

    log_returns = np.log(closes / closes.shift(1)).dropna()
    annualized_vol = (float(log_returns.std(ddof=1)) * np.sqrt(TRADING_DAYS_PER_YEAR) * 100
                      if len(log_returns) > 1 else None)
    sma50, sma200 = sma(50), sma(200)
    return {
        "observations": n,
        "last_close": round(last, 4),
        "first_date": str(closes.index[0])[:10],
        "last_date": str(closes.index[-1])[:10],
        "sma20": round(sma(20), 4) if sma(20) else None,
        "sma50": round(sma50, 4) if sma50 else None,
        "sma200": round(sma200, 4) if sma200 else None,
        "above_sma20": (last > sma(20)) if sma(20) else None,
        "above_sma50": (last > sma50) if sma50 else None,
        "above_sma200": (last > sma200) if sma200 else None,
        "momentum_30d_pct": round(momentum(30), 2) if momentum(30) is not None else None,
        "annualized_vol_pct": float(round(annualized_vol, 1)) if annualized_vol is not None else None,
    }


def build_forecast(history: pd.DataFrame | None, horizons: list[tuple[str, int]] | None = None
                  ) -> dict[str, Any]:
    """Project the close price forward over each horizon with an 80%/95% band.

    Returns {"available": False, ...} with an explicit reason when there is not
    enough history — callers are expected to render that state rather than
    hiding it, because "no forecast yet" is the truthful answer for a market
    where we have been collecting for days, not years."""
    horizons = horizons or DEFAULT_HORIZONS
    closes = _clean_closes(history)
    n = len(closes)

    trend = summarize_trend(history)
    base = {
        "available": False,
        "observations": n,
        "trend": trend,
        "disclaimer": DISCLAIMER,
        "method": "random walk with drift on log returns, drift shrunk by n/(n+20)",
    }

    if n < MIN_HISTORY:
        base["reason"] = (
            f"Only {n} daily close{'s' if n != 1 else ''} accumulated — "
            f"{MIN_HISTORY} needed before a projection is meaningful. History starts "
            f"on the first 16:00 EAT snapshot run; there is no backfill."
        )
        base["horizons"] = []
        return base

    log_returns = np.log(closes / closes.shift(1)).dropna()
    if len(log_returns) < 2 or float(log_returns.std(ddof=1)) == 0:
        base["reason"] = "Price series is flat or too short to estimate volatility."
        base["horizons"] = []
        return base

    last_price = float(closes.iloc[-1])
    raw_drift = float(log_returns.mean())
    daily_vol = float(log_returns.std(ddof=1))

    # Shrink drift toward zero. A 20-day sample contributes 50% of its raw
    # drift, a 60-day sample 75%, a 200-day sample 91%.
    shrink = n / (n + SHRINKAGE_K)
    drift = raw_drift * shrink

    points = []
    for label, days in horizons:
        centre = last_price * float(np.exp(drift * days))
        points.append({
            "label": label,
            "trading_days": days,
            "calendar_days": int(round(days * 365 / TRADING_DAYS_PER_YEAR)),
            "expected": round(centre, 2),
            "expected_pct": round((centre / last_price - 1) * 100, 1),
            "band_80": [round(last_price * float(np.exp(drift * days - Z_80 * daily_vol * np.sqrt(days))), 2),
                        round(last_price * float(np.exp(drift * days + Z_80 * daily_vol * np.sqrt(days))), 2)],
            "band_95": [round(last_price * float(np.exp(drift * days - Z_95 * daily_vol * np.sqrt(days))), 2),
                        round(last_price * float(np.exp(drift * days + Z_95 * daily_vol * np.sqrt(days))), 2)],
        })

    # Confidence reflects evidence, not model sophistication.
    if n < 60:
        confidence, confidence_note = "low", "Under 3 months of history — treat as indicative only."
    elif n < 200:
        confidence, confidence_note = "medium", "3-9 months of history — trend estimates are usable but not stable."
    else:
        confidence, confidence_note = "higher", "Over 9 months of history, including a full 200-day average."

    return {
        **base,
        "available": True,
        "last_price": round(last_price, 2),
        "as_of": trend.get("last_date"),
        "daily_drift_pct": round(drift * 100, 4),
        "raw_drift_pct": round(raw_drift * 100, 4),
        "drift_shrinkage": round(shrink, 3),
        "daily_vol_pct": round(daily_vol * 100, 3),
        "annualized_vol_pct": round(daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR) * 100, 1),
        "confidence": confidence,
        "confidence_note": confidence_note,
        "horizons": points,
    }


def direction_label(forecast: dict[str, Any]) -> str:
    """Plain-language read of the 3-month point estimate.

    Deliberately conservative: it takes the 95% band into account, so a
    wide band suppresses the call rather than reporting a confident lean."""
    if not forecast.get("available"):
        return "Not enough data"
    point = next((h for h in forecast["horizons"] if h["label"] == "3m"), None)
    if not point:
        return "Not enough data"
    low, high = point["band_95"]
    last = forecast.get("last_price") or 0
    if not last:
        return "Not enough data"
    if last > high:
        return "Point estimate above the 95% band"
    if last < low:
        return "Point estimate below the 95% band"
    if point["expected_pct"] > 5:
        return "Modest upward drift"
    if point["expected_pct"] < -5:
        return "Modest downward drift"
    return "Broadly flat"
