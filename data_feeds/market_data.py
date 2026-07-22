"""
OHLCV market data fetcher. Wraps ccxt so strategy code never talks to the exchange
directly — makes it trivial to swap exchanges or plug in a different data vendor
(e.g. for forex/gold via OANDA) without touching strategy logic.
"""
import time
import pandas as pd
import ccxt


class MarketData:
    def __init__(self, exchange_id: str, api_key: str = "", api_secret: str = ""):
        exchange_cls = getattr(ccxt, exchange_id)
        self.exchange = exchange_cls({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        })
        self._cache = {}
        self._cache_ttl = 30  # seconds — avoid hammering the API on every loop tick

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
        cache_key = (symbol, timeframe)
        now = time.time()
        if cache_key in self._cache:
            cached_at, df = self._cache[cache_key]
            if now - cached_at < self._cache_ttl:
                return df

        raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)

        self._cache[cache_key] = (now, df)
        return df

    def latest_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return ticker["last"]
