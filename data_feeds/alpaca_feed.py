"""
Alpaca Markets data feed for US Stocks, ETFs, and Commodities (via ETFs like GLD).

Alpaca has excellent market data APIs with commission-free execution. This wrapper
provides the same OHLCV interface as MarketData so strategy code stays exchange-agnostic.

Alpaca supports both paper and live trading with the same API — just different keys.
Paper trading keys are prefixed with 'PK-' and live with 'AK-'.

Note: Alpaca supports crypto too, but for this bot crypto goes through Binance/ccxt.
This feed is specifically for equities and ETFs.
"""
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta


ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_TRADE_URL = "https://paper-api.alpaca.markets"


TIMEFRAME_MAP = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m", "30Min": "30m",
    "1h": "1h", "1H": "1h", "1Day": "1D", "1d": "1D", "1W": "1W", "1M": "1M",
}

# Reverse map for going from ccxt-style timeframes to Alpaca
CCXT_TO_ALPACA = {
    "1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
    "1h": "1Hour", "4h": "4Hour", "1d": "1Day", "1w": "1Week", "1M": "1Month",
}


class AlpacaFeed:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.data_url = ALPACA_DATA_URL
        self.trade_url = ALPACA_TRADE_URL if paper else "https://api.alpaca.markets"
        self._cache = {}
        self._cache_ttl = 60  # Alpaca rate limits are generous, but still cache

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
        cache_key = (symbol, timeframe)
        now = time.time()
        if cache_key in self._cache:
            cached_at, df = self._cache[cache_key]
            if now - cached_at < self._cache_ttl:
                return df

        alpaca_tf = CCXT_TO_ALPACA.get(timeframe, "15Min")

        # Alpaca v2 bars endpoint
        url = f"{self.data_url}/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": alpaca_tf,
            "limit": min(limit, 10000),
            "adjustment": "split",  # adjusted for splits
            "feed": "iex",  # IEX feed — free; use "sip" for paid real-time
        }

        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        bars = data.get("bars", [])
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = []
        for b in bars:
            rows.append({
                "timestamp": pd.Timestamp(b["t"]),
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": int(b["v"]),
            })

        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        self._cache[cache_key] = (now, df)
        return df

    def latest_price(self, symbol: str) -> float:
        url = f"{self.data_url}/v2/stocks/{symbol}/trades/latest"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        trade = resp.json().get("trade", {})
        return float(trade.get("p", 0))

    def get_snapshot(self, symbol: str) -> dict:
        """Returns daily bar + latest trade + VWAP for a symbol — useful for
        dashboard display and quick checks without fetching full OHLCV."""
        url = f"{self.data_url}/v2/stocks/{symbol}/snapshot"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        snap = resp.json()
        latest = snap.get("latestTrade", {})
        daily = snap.get("dailyBar", {})
        return {
            "symbol": symbol,
            "latest_price": float(latest.get("p", 0)),
            "daily_open": float(daily.get("o", 0)),
            "daily_high": float(daily.get("h", 0)),
            "daily_low": float(daily.get("l", 0)),
            "daily_close": float(daily.get("c", 0)),
            "daily_volume": int(daily.get("v", 0)),
            "prev_daily_close": float(snap.get("prevDailyBar", {}).get("c", 0)),
        }

    def get_crypto_ohlcv(self, symbol: str, timeframe: str = "15m",
                          limit: int = 200) -> pd.DataFrame:
        """Alpaca also supports crypto data — use this if you want crypto
        through Alpaca instead of Binance. Symbol format: BTC/USD."""
        alpaca_tf = CCXT_TO_ALPACA.get(timeframe, "15Min")
        crypto_symbol = symbol.replace("/", "")  # BTC/USD -> BTCUSD

        url = f"{self.data_url}/v1beta3/crypto/us/bars"
        params = {
            "symbols": crypto_symbol,
            "timeframe": alpaca_tf,
            "limit": min(limit, 10000),
        }

        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        bars = data.get("bars", {}).get(crypto_symbol, [])
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = []
        for b in bars:
            rows.append({
                "timestamp": pd.Timestamp(b["t"]),
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": float(b["v"]),
            })

        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df
