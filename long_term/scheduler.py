"""
Runs the long-term investing jobs on a schedule and pushes results through
the notification pipeline + dashboard cache. Free by default (yfinance for
US/ETF, afx.kwayisi.org for NSE fundamentals) with one optional paid feed
(RapidAPI "Nairobi Stock Exchange (NSE)", NSE_RAPIDAPI_KEY) for the
exchange-wide price list.

Four jobs:
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
  4. NSE Kenya snapshot (nse.snapshot_schedule, default 16:00 Mon-Fri
     Africa/Nairobi) — the ONE job allowed to call RapidAPI. NSE trades
     09:00-15:00 EAT, so 16:00 EAT is after the close. It fetches every
     listed security in a single /stocks request, appends one daily close per
     ticker (building the chart and projection history), and rebuilds the
     whole-exchange dashboard panel.

Run as its own process:
    python long_term/scheduler.py
"""
import os
import sys
import threading
import yaml
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.util import astimezone
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

# NSE trades 09:00-15:00 East Africa Time. The 16:00 snapshot is expressed in
# EAT explicitly rather than in the host's local time, so it stays correct
# whether the VPS runs UTC or EAT — unlike the two jobs above, which are
# documented in UTC and follow the scheduler's default timezone.
NSE_TIMEZONE = astimezone("Africa/Nairobi")


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

    tickers = [(t, "us") for t in watchlist["us_stocks"]] + \
              [(t, "nse") for t in watchlist["nse_kenya"]]
    results = screener.screen_universe(tickers)

    passed = [r for r in results if r["passed"]]
    if not passed:
        notifier.notify("long_term_signal", "Weekly screen — no names passed all filters.")
        return

    message_lines = [f"Weekly screen: {len(passed)}/{len(results)} names passed."]
    for r in passed:
        sentiment = news.get_sentiment(r["ticker"])
        trend = screener.trend_context(r["ticker"], market=r["profile"].get("market"))
        message_lines.append(screener.format_alert(r, trend=trend, news=sentiment))

    notifier.notify_report("long_term_signal", "\n".join(message_lines),
                           subject="📊 Weekly stock screen")


def run_nse_snapshot(fundamentals, nse_feed, config: dict, spend_rapidapi: bool = True):
    """The 16:00 EAT NSE job: refresh the exchange snapshot from RapidAPI, then
    rebuild the whole-exchange dashboard panel.

    Order matters. refresh_market_snapshot() spends the one daily RapidAPI call
    and appends today's close to every ticker's history; only then does
    refresh_nse_dashboard() read that history, so the projections and charts
    include today. Neither step raises: a failed snapshot must not take the
    dashboard down, it should fall back to the previous day's data.

    spend_rapidapi=False rebuilds the panel from data already on disk WITHOUT
    calling the API. The startup path uses that flag: the Basic plan allows
    only 4 requests/hour and 250/month, and a restart loop (or a VPS that
    bounces daily) would otherwise burn a call every boot. Only the scheduled
    16:00 job may spend quota.
    """
    nse_cfg = config.get("nse", {})
    if not spend_rapidapi:
        print("NSE panel rebuild (no RapidAPI call): using the last persisted "
              "snapshot. The 16:00 EAT job owns the API budget.")
    elif not nse_feed.rapidapi_configured:
        print("NSE_RAPIDAPI_KEY is not set — skipping the RapidAPI snapshot. "
              "The dashboard will fall back to the free afx.kwayisi.org source.")
    else:
        try:
            snapshot = nse_feed.refresh_market_snapshot()
            if snapshot:
                count = len(snapshot.get("stocks") or [])
                print(f"NSE snapshot refreshed: {count} securities "
                      f"({snapshot.get('trading_date')}, source={snapshot.get('source')}).")
        except Exception as e:
            print(f"NSE snapshot refresh failed, keeping previous data: {e}")

    try:
        result = daily_digest.refresh_nse_dashboard(config, fundamentals, nse_feed)
        counts = result.get("counts", {})
        print(f"NSE dashboard cache rebuilt: {counts.get('total', 0)} securities, "
              f"{counts.get('with_fundamentals', 0)} with fundamentals, "
              f"{counts.get('with_forecasts', 0)} with projections.")
    except Exception as e:
        print(f"NSE dashboard rebuild failed: {e}")


def main():
    notifier = build_notifier()
    fundamentals = FundamentalsFetcher()
    nse_feed = NSEFeed()
    lt_cfg = CONFIG.get("long_term", {})
    nse_cfg = CONFIG.get("nse", {})

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
    # 16:00 EAT, Mon-Fri, pinned to Africa/Nairobi so it does not drift with
    # the host clock. misfire_grace_time matters here: if the process is
    # restarting or the VPS was down at 16:00, still take today's close rather
    # than silently skipping a day of history.
    scheduler.add_job(
        lambda: run_nse_snapshot(fundamentals, nse_feed, CONFIG),
        CronTrigger.from_crontab(
            nse_cfg.get("snapshot_schedule", "0 16 * * MON-FRI"),
            timezone=NSE_TIMEZONE,
        ),
        id="nse_snapshot",
        misfire_grace_time=nse_cfg.get("misfire_grace_minutes", 180) * 60,
        coalesce=True,
        max_instances=1,
    )

    # Prime the dashboard cache immediately instead of waiting up to an hour
    # for the first interval tick.
    try:
        daily_digest.refresh_dashboard_cache(CONFIG, nse_feed)
    except Exception as e:
        print(f"Initial dashboard cache refresh failed (will retry hourly): {e}")

    # The full stock analysis normally runs with the daily digest; if the
    # dashboard has none (first deploy) or it's stale, build it now in the
    # background (~3 min) rather than waiting for tomorrow's digest.
    cached = daily_digest.read_cache().get("analysis", {}).get("updated_at")
    age_h = ((datetime.now(timezone.utc) - datetime.fromisoformat(cached)).total_seconds() / 3600
             if cached else None)
    if age_h is None or age_h > 20:
        threading.Thread(
            target=lambda: daily_digest.refresh_analysis(CONFIG, fundamentals, nse_feed),
            daemon=True,
        ).start()

    # The NSE panel is expensive to build (one fundamentals request per listed
    # security), so only rebuild it on startup when there is none or today's is
    # missing — otherwise the 16:00 job owns it and the dashboard just reads
    # the file. spend_rapidapi=False: a restart must never spend one of the
    # 4/hour, 250/month calls, so this rebuilds from the last persisted
    # snapshot (or the free AFX fallback) only.
    nse_cache = daily_digest.read_nse_cache()
    nse_updated = nse_cache.get("updated_at")
    nse_age_h = ((datetime.now(timezone.utc) - datetime.fromisoformat(nse_updated)).total_seconds() / 3600
                 if nse_updated else None)
    if nse_age_h is None or nse_age_h > 20:
        threading.Thread(
            target=lambda: run_nse_snapshot(fundamentals, nse_feed, CONFIG,
                                            spend_rapidapi=False),
            daemon=True,
        ).start()

    print("Long-term scheduler started: weekly screen, daily digest, hourly "
          "dashboard refresh, and the 16:00 EAT NSE snapshot.")
    scheduler.start()


if __name__ == "__main__":
    main()
