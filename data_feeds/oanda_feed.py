"""
OANDA REST API data feed for Forex and Commodities (XAU/USD).

OANDA uses its own candle endpoint — ccxt's OANDA support is limited and
inconsistent for v20 REST. This is a direct, thin wrapper around the OANDA
candles endpoint, matching the same interface contract as MarketData so
strategy code never sees the difference.

OANDA instrument naming: EUR_USD, XAU_USD (underscores, not slashes).
We accept both "EUR/USD" (ccxt-style) and "EUR_USD" (OANDA-style) and
normalize internally.
"""
import time
import requests
import pandas as pd
from datetime import datetime, timezone


OANDA_API_URL = "https://api-fxtrade.oanda.com"
OANDA_STREAM_URL = "https://stream-fxtrade.oanda.com"

# OANDA granularity to minutes mapping for cache TTL
GRANULARITY_MINUTES = {
    "S5": 0.08, "S10": 0.17, "S15": 0.25, "S30": 0.5,
    "M1": 1, "M2": 2, "M4": 4, "M5": 5, "M10": 10,
    "M15": 15, "M30": 30, "H1": 60, "H2": 120, "H3": 180,
    "H4": 240, "H6": 360, "H8": 480, "H12": 720,
    "D": 1440, "W": 10080, "M": 43200,
}

TIMEFRAME_TO_GRANULARITY = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
    "1h": "H1", "4h": "H4", "1d": "D", "1w": "W", "1M": "M",
}


class OandaFeed:
    def __init__(self, api_key: str, account_id: str, practice: bool = False):
        self.api_key = api_key
        self.account_id = account_id
        base = "https://api-fxpractice.oanda.com" if practice else OANDA_API_URL
        self.base_url = base
        self._cache = {}
        self._cache_ttl = 30

    def _normalize_instrument(self, symbol: str) -> str:
        """Convert 'EUR/USD' -> 'EUR_USD', pass through 'EUR_USD' as-is."""
        return symbol.replace("/", "_")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
        cache_key = (symbol, timeframe)
        now = time.time()
        if cache_key in self._cache:
            cached_at, df = self._cache[cache_key]
            if now - cached_at < self._cache_ttl:
                return df

        instrument = self._normalize_instrument(symbol)
        granularity = TIMEFRAME_TO_GRANULARITY.get(timeframe, "M15")

        # OANDA candles endpoint — max 5000 per request
        params = {
            "granularity": granularity,
            "count": min(limit, 5000),
            "price": "MBA",  # Mid, Bid, Ask — we use Mid for OHLCV
        }

        url = f"{self.base_url}/v3/instruments/{instrument}/candles"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        rows = []
        for c in data.get("candles", []):
            if not c["complete"] and not c["mid"]:
                continue
            mid = c["mid"]
            rows.append({
                "timestamp": pd.Timestamp(c["time"]),
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": int(c["volume"]),
            })

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        self._cache[cache_key] = (now, df)
        return df

    def latest_price(self, symbol: str) -> float:
        instrument = self._normalize_instrument(symbol)
        url = f"{self.base_url}/v3/accounts/{self.account_id}/pricing"
        resp = requests.get(url, headers=self._headers(),
                           params={"instruments": instrument}, timeout=10)
        resp.raise_for_status()
        prices = resp.json().get("prices", [])
        if prices:
            return float(prices[0]["bids"][0]["price"])
        raise ValueError(f"No price returned for {instrument}")

    def get_current_spread(self, symbol: str) -> dict:
        """Returns bid/ask/spread for a forex instrument — useful for
        calibrating slippage expectations in the risk manager."""
        instrument = self._normalize_instrument(symbol)
        url = f"{self.base_url}/v3/accounts/{self.account_id}/pricing"
        resp = requests.get(url, headers=self._headers(),
                           params={"instruments": instrument}, timeout=10)
        resp.raise_for_status()
        prices = resp.json().get("prices", [])
        if prices:
            p = prices[0]
            bid = float(p["bids"][0]["price"])
            ask = float(p["asks"][0]["price"])
            return {"bid": bid, "ask": ask, "spread": ask - bid,
                    "spread_pips": (ask - bid) * (10000 if "JPY" not in instrument else 100)}
        raise ValueError(f"No price returned for {instrument}")
