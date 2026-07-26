"""
Market intelligence layer — provides regime detection, Fear & Greed index,
Dollar Index (DXY) trend, and cross-asset correlation signals.

This module feeds into the strategy ensemble as an additional filter:
- Risk-off regimes reduce position sizes or block trades entirely.
- Extreme Fear/Greed readings adjust entry thresholds.
- DXY trend impacts forex pair selection.
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

    def get_fear_greed(self) -> dict | None:
        """Fetch Crypto Fear & Greed Index from alternative.me API."""
        if not self.cfg.get("fear_greed_enabled", True):
            return None

        cache_key = "fear_greed"
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return data

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
                return result
        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")

        return None

    def get_dxy_trend(self, feed_router=None) -> dict | None:
        """Estimate Dollar Index (DXY) trend using USD-based pairs as proxy.
        DXY itself may not be available via free APIs, so we use EUR/USD inverse
        as a proxy (EUR/USD is ~57% of DXY)."""
        if not self.cfg.get("dxy_enabled", True):
            return None

        cache_key = "dxy_trend"
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return data

        try:
            if feed_router is None:
                return None

            # Use EUR/USD as DXY proxy (inverse correlation)
            df = feed_router.get_ohlcv("EUR/USD", timeframe="1h", limit=50)
            if df is None or len(df) < 20:
                return None

            ema_20 = df["close"].ewm(span=20, adjust=False).mean()
            ema_50 = df["close"].ewm(span=50, adjust=False).mean()

            last_price = float(df.iloc[-1]["close"])
            last_ema20 = float(ema_20.iloc[-1])
            last_ema50 = float(ema_50.iloc[-1])

            # EUR/USD rising = DXY weakening, EUR/USD falling = DXY strengthening
            if last_price < last_ema20 < last_ema50:
                trend = "strengthening"  # DXY strong (bearish for gold, mixed for crypto)
            elif last_price > last_ema20 > last_ema50:
                trend = "weakening"  # DXY weak (bullish for gold, bullish for crypto)
            else:
                trend = "neutral"

            change_24h = ((last_price - float(df.iloc[-24]["close"])) / float(df.iloc[-24]["close"]) * 100) \
                if len(df) >= 24 else 0

            result = {
                "trend": trend,
                "eur_usd": last_price,
                "change_24h_pct": round(change_24h, 3),
            }
            self._cache[cache_key] = (time.time(), result)
            return result

        except Exception as e:
            logger.warning(f"DXY trend estimation failed: {e}")
            return None

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
