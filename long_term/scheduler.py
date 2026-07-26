"""
Runs the long-term equity screener on a schedule and pushes results through
the notification pipeline. Uses the Alpaca feed for US stock data and the
Alpaca feed's daily bars for trend analysis.

Run as its own process:
    python long_term/scheduler.py
"""
import os
import sys
import yaml
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent))
from long_term.fundamentals import FundamentalsFetcher
from long_term.screener import EquityScreener
from long_term.news_sentiment import NewsSentiment
from alerts.notifier import Notifier

load_dotenv()

with open(Path(__file__).parent.parent / "config" / "config.yaml") as f:
    CONFIG = yaml.safe_load(f)

WATCHLIST = {
    "sp500": ["AAPL", "MSFT", "JNJ", "KO", "PG"],
    "nse_kenya": ["SCOM", "EQTY", "KCB"],
}


def build_notifier():
    return Notifier(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"),
        email_cfg={
            "address": os.environ.get("EMAIL_ADDRESS"),
            "app_password": os.environ.get("EMAIL_APP_PASSWORD"),
            "to": os.environ.get("EMAIL_TO"),
            "smtp_host": os.environ.get("EMAIL_SMTP_HOST"),
            "smtp_port": int(os.environ.get("EMAIL_SMTP_PORT", 587)),
        },
    )


def _build_market_data_fn():
    """Build a market data function using Alpaca feed for daily bars."""
    from data_feeds.alpaca_feed import AlpacaFeed
    alpaca_key = os.environ.get("ALPACA_API_KEY")
    alpaca_secret = os.environ.get("ALPACA_API_SECRET")
    if alpaca_key and alpaca_secret:
        paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
        feed = AlpacaFeed(alpaca_key, alpaca_secret, paper=paper)
        def _get_daily(ticker):
            try:
                return feed.get_ohlcv(ticker, timeframe="1D", limit=250)
            except Exception:
                return None
        return _get_daily

    # Fallback: try ccxt Binance (only works for crypto)
    from data_feeds.market_data import MarketData
    binance_key = os.environ.get("BINANCE_API_KEY", "")
    binance_secret = os.environ.get("BINANCE_API_SECRET", "")
    if binance_key:
        feed = MarketData("binance", binance_key, binance_secret)
        def _get_crypto(ticker):
            try:
                return feed.get_ohlcv(f"{ticker}/USDT", timeframe="1d", limit=250)
            except Exception:
                return None
        return _get_crypto

    return lambda t: None


def run_screen():
    notifier = build_notifier()
    fundamentals = FundamentalsFetcher()
    market_data_fn = _build_market_data_fn()
    screener = EquityScreener(CONFIG, fundamentals, market_data_fn)
    news = NewsSentiment()

    all_tickers = WATCHLIST.get("sp500", []) + WATCHLIST.get("nse_kenya", [])
    results = screener.screen_universe(all_tickers)

    passed = [r for r in results if r["passed"]]
    if not passed:
        notifier.notify("long_term_signal", "Weekly screen — no names passed all filters.")
        return

    message_lines = [f"Weekly screen: {len(passed)}/{len(results)} names passed."]
    for r in passed:
        sentiment = news.get_sentiment(r["ticker"])
        trend = screener.trend_context(r["ticker"])
        message_lines.append(screener.format_alert(r, trend=trend, news=sentiment))

    notifier.notify("long_term_signal", "\n".join(message_lines))


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(run_screen, CronTrigger.from_crontab(CONFIG["long_term"]["rebalance_alert_schedule"]))
    print("Long-term screener scheduler started.")
    scheduler.start()
