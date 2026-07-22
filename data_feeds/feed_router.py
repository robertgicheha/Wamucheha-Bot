"""
Multi-asset feed router.

Routes symbol requests to the correct data feed based on asset class:
- Crypto (BTC/USDT, ETH/USDT) -> ccxt via MarketData (Binance)
- Forex (EUR/USD, GBP/USD) -> OANDA
- Commodities (XAU/USD, XAG/USD) -> OANDA
- US Stocks/ETFs (AAPL, SPY, GLD) -> Alpaca
- Kenyan Stocks (SCOM, EQTY) -> NSE scraper

Strategy code should use this router instead of calling a specific feed directly.
This way, adding a new asset class or swapping providers only changes config,
not strategy code.
"""
import os
from data_feeds.market_data import MarketData
from data_feeds.oanda_feed import OandaFeed
from data_feeds.alpaca_feed import AlpacaFeed
from data_feeds.nse_feed import NSEFeed


# Forex instruments that OANDA handles
OANDA_INSTRUMENTS = {
    "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "NZD/USD",
    "USD/CAD", "USD/CHF", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/NZD", "EUR/AUD", "GBP/AUD", "USD/SGD", "USD/HKD",
}

# Commodities via OANDA
OANDA_COMMODITIES = {"XAU/USD", "XAG/USD", "XAU_USD", "XAG_USD"}

# NSE tickers — anything ending with .NSE or matching known NSE codes
NSE_TICKERS = {"SCOM", "EQTY", "KCB", "BAT", "EABL", "SAFARICOM", "DTK",
               "COOP", "ABSA", "KNC", "NIC", "KEGN", "I&M", "HFCK"}


class FeedRouter:
    def __init__(self, config: dict):
        self.feeds = {}
        self._init_feeds(config)

    def _init_feeds(self, config: dict):
        # ccxt feeds (Binance, OKX, etc.)
        for ex_cfg in config.get("execution", {}).get("exchanges", []):
            if not ex_cfg.get("enabled"):
                continue
            name = ex_cfg["name"]
            if name in ("binance", "okx", "kraken", "coinbase", "bybit"):
                api_key = os.environ.get(f"{name.upper()}_API_KEY", "")
                api_secret = os.environ.get(f"{name.upper()}_API_SECRET", "")
                self.feeds[name] = MarketData(name, api_key, api_secret)

        # OANDA feed for forex/commodities
        oanda_key = os.environ.get("OANDA_API_KEY")
        oanda_account = os.environ.get("OANDA_ACCOUNT_ID")
        if oanda_key and oanda_account:
            practice = os.environ.get("OANDA_PRACTICE", "true").lower() == "true"
            self.feeds["oanda"] = OandaFeed(oanda_key, oanda_account, practice=practice)

        # Alpaca feed for US stocks/ETFs
        alpaca_key = os.environ.get("ALPACA_API_KEY")
        alpaca_secret = os.environ.get("ALPACA_API_SECRET")
        if alpaca_key and alpaca_secret:
            paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
            self.feeds["alpaca"] = AlpacaFeed(alpaca_key, alpaca_secret, paper=paper)

        # NSE feed for Kenyan stocks
        apify_token = os.environ.get("APIFY_TOKEN")
        self.feeds["nse"] = NSEFeed(apify_token=apify_token)

    def _classify_symbol(self, symbol: str) -> str:
        """Returns the feed name to use for a given symbol."""
        clean = symbol.upper().replace("_", "/")

        # Explicit .NSE suffix
        if ".NSE" in symbol.upper():
            return "nse"

        # Known NSE tickers (without suffix)
        ticker = symbol.split("/")[0].split(".")[0].upper()
        if ticker in NSE_TICKERS:
            return "nse"

        # OANDA forex/commodities
        if clean in OANDA_INSTRUMENTS or clean in OANDA_COMMODITIES:
            return "oanda"

        # US stocks/ETFs: 1-5 uppercase letters (not a known crypto pair)
        if "/" not in symbol and ticker.isalpha() and len(ticker) <= 5:
            # Check if it's a known crypto pair first
            return "alpaca"

        # Default: ccxt (crypto)
        # Find the first enabled ccxt exchange
        for name, feed in self.feeds.items():
            if isinstance(feed, MarketData):
                return name
        return "binance"

    def get_feed(self, symbol: str):
        """Returns the appropriate feed object for a symbol."""
        feed_name = self._classify_symbol(symbol)
        return self.feeds.get(feed_name)

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200):
        """Unified OHLCV fetch — routes to correct feed based on symbol."""
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
        result = {}
        for name, feed in self.feeds.items():
            result[name] = {
                "type": type(feed).__name__,
                "ready": True,
            }
        return result
