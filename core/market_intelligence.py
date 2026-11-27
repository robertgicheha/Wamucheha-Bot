"""
Market intelligence layer — provides regime detection, Fear & Greed index,
Dollar Index (DXY) trend, and cross-asset correlation signals.

This module feeds into the strategy ensemble as an additional filter:
- Risk-off regimes reduce position sizes or block trades entirely.
- Extreme Fear/Greed readings adjust entry thresholds.
- DXY trend matters for gold (strong dollar is a headwind) and risk assets.
"""
import os
import json
import time
import logging
from pathlib import Path

import requests
import pandas as pd
import numpy as np

logger = logging.getLogger("market_intelligence")

CACHE_DIR = Path(__file__).parent.parent / "data" / "intel_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class MarketIntelligence:
    def __init__(self, config: dict):
        self.cfg = config.get("intelligence", {})
        self._cache = {}
        self._cache_ttl = 300  # 5 minutes
        self._last_good = {}   # key -> (unix time, reading)

    def _fallback(self, key: str, max_age: int) -> dict | None:
        """The last successful reading, if it is still recent enough to use."""
        stamp, data = self._last_good.get(key, (0, None))
        return data if data and time.time() - stamp < max_age else None

    def get_fear_greed(self) -> dict | None:
        """Fetch Crypto Fear & Greed Index from alternative.me API."""
        if not self.cfg.get("fear_greed_enabled", True):
            return None

        cache_key = "fear_greed"
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return data
        failure = "no data returned"

        try:
            resp = requests.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=10,
            )
            resp.raise_for_status()
            entries = resp.json().get("data", [])
            if entries:
                entry = entries[0]
                result = {
                    "value": int(entry["value"]),
                    "classification": entry["value_classification"],
                    "timestamp": entry["timestamp"],
                }
                self._cache[cache_key] = (time.time(), result)
                self._last_good[cache_key] = (time.time(), result)
                return result
        except Exception as e:
            failure = str(e)

        # Cached like a success, so an outage costs one attempt per 5 minutes and not one per loop.
        # The index is published once a day, so yesterday's reading is still meaningful.
        result = self._fallback(cache_key, 24 * 3600)
        logger.warning(f"Fear & Greed fetch failed ({'using the last reading' if result else 'skipping it'}), "
                       f"retrying in 5 min: {failure}")
        self._cache[cache_key] = (time.time(), result)
        return result

    def get_dxy_trend(self, feed_router=None) -> dict | None:
        """Dollar Index (DXY) trend from free yfinance data (ticker DX-Y.NYB).

        Failures are cached for 10 minutes so a blocked network costs one quick
        attempt, not one per trading-loop tick."""
        if not self.cfg.get("dxy_enabled", True):
            return None

        cache_key = "dxy_trend"
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < (self._cache_ttl if data else 600):
                return data

        result = None
        try:
            import yfinance as yf
            df = yf.Ticker("DX-Y.NYB").history(period="10d", interval="1h", timeout=8)
            if df is not None and len(df) >= 50:
                close = df["Close"]
                ema_20 = close.ewm(span=20, adjust=False).mean()
                ema_50 = close.ewm(span=50, adjust=False).mean()
                last, e20, e50 = float(close.iloc[-1]), float(ema_20.iloc[-1]), float(ema_50.iloc[-1])
                # Dollar rising is a headwind for gold and crypto; falling is a tailwind.
                if last > e20 > e50:
                    trend = "strengthening"
                elif last < e20 < e50:
                    trend = "weakening"
                else:
                    trend = "neutral"
                prev = float(close.iloc[-24]) if len(close) >= 24 else last
                result = {"trend": trend, "dxy": round(last, 3),
                          "change_24h_pct": round((last - prev) / prev * 100, 3)}
                self._last_good[cache_key] = (time.time(), result)
            else:
                logger.warning("DXY trend: Yahoo returned too little data (usually temporary)")
        except Exception as e:
            logger.warning(f"DXY trend unavailable: {e}")
        if result is None:
            result = self._fallback(cache_key, 6 * 3600)
        self._cache[cache_key] = (time.time(), result)
        return result

    def get_market_regime(self, feed_router=None) -> dict:
        """Determine overall market regime from multiple signals."""
        fng = self.get_fear_greed()
        dxy = self.get_dxy_trend(feed_router)

        signals = []

        if fng:
            val = fng["value"]
            if val < 25:
                signals.append(-2)
            elif val < 40:
                signals.append(-1)
            elif val > 75:
                signals.append(2)
            elif val > 60:
                signals.append(1)
            else:
                signals.append(0)

        if dxy:
            if dxy["trend"] == "strengthening":
                signals.append(-1)
            elif dxy["trend"] == "weakening":
                signals.append(1)
            else:
                signals.append(0)

        avg_signal = sum(signals) / len(signals) if signals else 0

        if avg_signal >= 0.5:
            regime = "risk_on"
        elif avg_signal <= -0.5:
            regime = "risk_off"
        else:
            regime = "neutral"

        result = {
            "regime": regime,
            "fear_greed": fng,
            "dxy_trend": dxy["trend"] if dxy else None,
            "dxy_change_24h": dxy["change_24h_pct"] if dxy else None,
            "score": round(avg_signal, 2),
        }

        # Write to cache for dashboard
        try:
            import json as _json
            cache_file = CACHE_DIR / "regime.json"
            cache_file.write_text(_json.dumps(result, default=str))
        except Exception:
            pass

        return result

    def get_correlation_signal(self, feed_router, symbol: str) -> dict | None:
        """Cross-asset correlation check: is the symbol moving with or against
        the broader market? Positive correlation with BTC/ETH during risk-on
        is normal. Divergence may signal opportunity or danger."""
        if not feed_router:
            return None

        try:
            # Fetch BTC as market benchmark
            df_sym = feed_router.get_ohlcv(symbol, timeframe="1h", limit=100)
            df_btc = feed_router.get_ohlcv("BTC/USDT", timeframe="1h", limit=100)

            if df_sym is None or df_btc is None:
                return None
            if len(df_sym) < 50 or len(df_btc) < 50:
                return None

            # Align on timestamps
            common_idx = df_sym.index.intersection(df_btc.index)
            if len(common_idx) < 30:
                return None

            sym_returns = df_sym.loc[common_idx, "close"].pct_change().dropna()
            btc_returns = df_btc.loc[common_idx, "close"].pct_change().dropna()

            correlation = sym_returns.corr(btc_returns)

            return {
                "symbol": symbol,
                "btc_correlation": round(correlation, 3),
                "divergent": abs(correlation) < 0.3,
            }
        except Exception:
            return None
