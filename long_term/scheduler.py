"""
Runs the long-term investing jobs on a schedule and pushes results through
the notification pipeline + dashboard cache. Free by default (yfinance for
US/ETF, afx.kwayisi.org for NSE Kenya) — no broker/API keys required.

Three jobs:
  1. Weekly deep screen (long_term.rebalance_alert_schedule, default Monday
     8am UTC) — the original full fundamentals+trend+sentiment writeup per
     passing name.
  2. Daily digest (long_term.daily_digest_schedule, default 05:30 UTC) —
     buy candidates, sell/review candidates, and today's gainers/losers,
     sent as one consolidated Telegram/Discord/email message.
  3. Hourly dashboard refresh (long_term.dashboard_refresh_interval_minutes,
     default 60) — cheap price/gainers-losers-only cache refresh so the
     dashboard has fresh data between daily digests, without repeating the
     fundamentals calls the daily job does.

Run as its own process:
    python long_term/scheduler.py
"""
import os
import sys
import yaml
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent))
from long_term.fundamentals import FundamentalsFetcher
from long_term.screener import EquityScreener
from long_term.news_sentiment import NewsSentiment
from long_term import daily_digest
from data_feeds.nse_feed import NSEFeed
from alerts.notifier import Notifier

load_dotenv()

with open(Path(__file__).parent.parent / "config" / "config.yaml") as f:
    CONFIG = yaml.safe_load(f)


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


def run_weekly_screen(fundamentals, nse_feed, notifier):
    watchlist = daily_digest.build_watchlist(CONFIG)
    market_data_fn = daily_digest.make_market_data_fn(nse_feed)
    screener = EquityScreener(CONFIG, fundamentals, market_data_fn)
    news = NewsSentiment()

    tickers = [(t, None) for t in watchlist["us_stocks"]] + \
              [(t, "nse") for t in watchlist["nse_kenya"]]
    results = screener.screen_universe(tickers)

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


def main():
    notifier = build_notifier()
    fundamentals = FundamentalsFetcher()
    nse_feed = NSEFeed()
    lt_cfg = CONFIG.get("long_term", {})

    scheduler = BlockingScheduler()

    scheduler.add_job(
        lambda: run_weekly_screen(fundamentals, nse_feed, notifier),
        CronTrigger.from_crontab(lt_cfg.get("rebalance_alert_schedule", "0 8 * * MON")),
    )
    scheduler.add_job(
        lambda: daily_digest.run_daily_digest(CONFIG, fundamentals, nse_feed, notifier),
        CronTrigger.from_crontab(lt_cfg.get("daily_digest_schedule", "30 5 * * *")),
    )
    scheduler.add_job(
        lambda: daily_digest.refresh_dashboard_cache(CONFIG, nse_feed),
        IntervalTrigger(minutes=lt_cfg.get("dashboard_refresh_interval_minutes", 60)),
    )

    # Prime the dashboard cache immediately instead of waiting up to an hour
    # for the first interval tick.
    try:
        daily_digest.refresh_dashboard_cache(CONFIG, nse_feed)
    except Exception as e:
        print(f"Initial dashboard cache refresh failed (will retry hourly): {e}")

    print("Long-term scheduler started: weekly screen, daily digest, hourly dashboard refresh.")
    scheduler.start()


if __name__ == "__main__":
    main()
