"""
MetaTrader 5 data feed.

Fetches OHLCV data from the MT5 terminal for any symbol available
on the connected broker (forex, metals, crypto, indices, etc.).
"""
import pandas as pd
from datetime import datetime, timezone

from core.mt5_client import get_mt5


# Names of the MetaTrader5 TIMEFRAME_* constants (copy_rates_* needs the constant, not a string).
TIMEFRAME_MAP = {
    "1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1", "1w": "TIMEFRAME_W1",
    "1M": "TIMEFRAME_MN1",
}


class MT5Feed:
    def __init__(self, login: int = 0, password: str = "", server: str = ""):
        self.login = login
        self.password = password
        self.server = server
        self._connected = False

    def connect(self) -> bool:
        try:
            mt5 = get_mt5()
            if not mt5.initialize():
                return False
        except Exception:  # ImportError on non-Windows, or MT5 bridge unreachable
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
            mt5 = get_mt5()
        except ImportError:
            raise ImportError("MetaTrader5 package not installed. Run: pip install MetaTrader5")

        if not self._connected:
            self.connect()

        mt5_tf = getattr(mt5, TIMEFRAME_MAP.get(timeframe, "TIMEFRAME_M15"))

        mt5.symbol_select(symbol, True)  # symbol must be in Market Watch
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
            mt5 = get_mt5()
        except ImportError:
            raise ImportError("MetaTrader5 package not installed")

        if not self._connected:
            self.connect()

        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"No price data for {symbol}")
        return (tick.bid + tick.ask) / 2.0
