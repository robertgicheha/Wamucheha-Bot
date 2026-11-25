"""
Feed router: picks the right data source for a symbol.

Scope is deliberately narrow, crypto and gold only:
- Crypto pairs written BASE/QUOTE (BTC/USDT, ETH/USDT, PAXG/USDT) -> ccxt exchange feed
- Anything without a slash (XAUUSD gold, or BTCUSD / ETHUSD crypto CFDs when the
  broker lists them)                                                -> MetaTrader 5 feed
"""
import os

from data_feeds.market_data import MarketData
from data_feeds.mt5_feed import MT5Feed

# MT5 symbols (no slash): gold, in the spellings brokers use.
GOLD_SYMBOLS = {"XAUUSD", "GOLD", "XAUUSDM", "XAUUSD."}
CCXT_EXCHANGES = ("binance", "okx", "kraken", "coinbase", "bybit")


class FeedRouter:
    def __init__(self, config: dict):
        self.feeds = {}
        self._init_feeds(config)

    def _init_feeds(self, config: dict):
        # ccxt feeds (public market data; API keys are optional for reading candles)
        for ex_cfg in config.get("execution", {}).get("exchanges", []):
            if not ex_cfg.get("enabled"):
                continue
            name = ex_cfg["name"]
            if name in CCXT_EXCHANGES:
                api_key = os.environ.get(f"{name.upper()}_API_KEY", "")
                api_secret = os.environ.get(f"{name.upper()}_API_SECRET", "")
                self.feeds[name] = MarketData(name, api_key, api_secret)

        # MT5 feed for gold
        mt5_login = int(os.environ.get("MT5_LOGIN", "0") or "0")
        if mt5_login and any(e.get("name") == "mt5" and e.get("enabled")
                             for e in config.get("execution", {}).get("exchanges", [])):
            mt5_feed = MT5Feed(login=mt5_login, password=os.environ.get("MT5_PASSWORD", ""),
                               server=os.environ.get("MT5_SERVER", ""))
            if mt5_feed.connect():
                self.feeds["mt5"] = mt5_feed

    def _classify_symbol(self, symbol: str) -> str:
        """Returns the feed name to use for a given symbol."""
        if "/" not in symbol:
            return "mt5"
        # Anything BASE/QUOTE is a crypto pair: first enabled ccxt exchange.
        for name, feed in self.feeds.items():
            if isinstance(feed, MarketData):
                return name
        return "binance"

    def get_feed(self, symbol: str):
        """Returns the appropriate feed object for a symbol."""
        return self.feeds.get(self._classify_symbol(symbol))

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200):
        """Unified OHLCV fetch, routed by symbol."""
        feed = self.get_feed(symbol)
        if feed is None:
            raise ValueError(f"No data feed available for symbol: {symbol}")
        return feed.get_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def latest_price(self, symbol: str) -> float:
        """Unified latest price fetch."""
        feed = self.get_feed(symbol)
        if feed is None:
            raise ValueError(f"No data feed available for symbol: {symbol}")
        return feed.latest_price(symbol)

    def get_available_feeds(self) -> dict:
        """Returns which feeds are configured and ready."""
        return {name: {"type": type(feed).__name__, "ready": True} for name, feed in self.feeds.items()}
