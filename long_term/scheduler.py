"""
Runs the long-term equity screener on a schedule (default: Monday mornings, per
config.yaml `long_term.rebalance_alert_schedule`) and pushes results through the
same Notifier used by the trading engine — one alert pipeline for everything.

Run this as its own process/systemd service (it's lightweight, no need to share a
process with the live trading engine):
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

# Populate with your actual watchlist. NSE tickers won't have fundamentals coverage
# from FMP/Alpha Vantage — maintain those manually or via a broker data export.
WATCHLIST = {
    "sp500": ["AAPL", "MSFT", "JNJ", "KO", "PG"],   # example dividend-relevant names
    "nse_kenya": ["SCOM", "EQTY", "KCB"],           # placeholder — verify tickers/coverage
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


def dummy_market_data_fn(ticker):
    """Placeholder — wire this to a real daily-bar data source (e.g. FMP historical
    price endpoint, yfinance, or your broker's API) before relying on trend_context()."""
    return None


def run_screen():
    notifier = build_notifier()
    fundamentals = FundamentalsFetcher()
    screener = EquityScreener(CONFIG, fundamentals, dummy_market_data_fn)
    news = NewsSentiment()

    all_tickers = WATCHLIST["sp500"] + WATCHLIST["nse_kenya"]
    results = screener.screen_universe(all_tickers)

    passed = [r for r in results if r["passed"]]
    if not passed:
        notifier.notify("long_term_signal", "Weekly screen ran — no names passed all filters this week.")
        return

    message_lines = [f"Weekly screen: {len(passed)}/{len(results)} names passed."]
    for r in passed:
        sentiment = news.get_sentiment(r["ticker"])
        message_lines.append(screener.format_alert(r, news=sentiment))

    notifier.notify("long_term_signal", "\n".join(message_lines))


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(run_screen, CronTrigger.from_crontab(CONFIG["long_term"]["rebalance_alert_schedule"]))
    print("Long-term screener scheduler started. Waiting for next scheduled run...")
    print("Run once immediately for testing with: python -c \"from long_term.scheduler import run_screen; run_screen()\"")
    scheduler.start()
