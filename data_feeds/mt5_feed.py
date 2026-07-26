"""
MetaTrader 5 data feed.

Fetches OHLCV data from the MT5 terminal for any symbol available
on the connected broker (forex, metals, crypto, indices, etc.).
"""
import pandas as pd
from datetime import datetime, timezone


TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w", "1M": "1M",
}


class MT5Feed:
    def __init__(self, login: int = 0, password: str = "", server: str = ""):
        self.login = login
        self.password = password
        self.server = server
        self._connected = False

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return False

        if not mt5.initialize():
            return False

        if self.login:
            authorized = mt5.login(self.login, password=self.password, server=self.server)
            if not authorized:
                mt5.shutdown()
                return False

        self._connected = True
        return True

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise ImportError("MetaTrader5 package not installed. Run: pip install MetaTrader5")

        if not self._connected:
            self.connect()

        mt5_tf = TIMEFRAME_MAP.get(timeframe, "15m")

        rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, limit)
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rates)
        df.rename(columns={
            "time": "timestamp",
            "open": "open",
            "high": "high",
            "low": "low",
            "tick_volume": "volume",
        }, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        return df[["open", "high", "low", "close", "volume"]]

    def latest_price(self, symbol: str) -> float:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise ImportError("MetaTrader5 package not installed")

        if not self._connected:
            self.connect()

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"No price data for {symbol}")
        return (tick.bid + tick.ask) / 2.0
