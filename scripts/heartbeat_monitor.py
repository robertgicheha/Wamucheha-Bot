"""
Run this on your LOCAL machine, not the VPS.

Why a separate watchdog matters: if the VPS bot process freezes, loses network, or
the whole VPS goes down, the bot obviously can't alert you about its own failure.
This script polls the VPS /health endpoint from an independent machine, so a VPS
outage is exactly the scenario it's designed to catch.

If the VPS is unreachable or hasn't updated its state in HEARTBEAT_ALERT_AFTER_MINUTES,
it fires its own Telegram/Discord/email alert using the same Notifier class, so you
get paged even when the primary bot cannot page itself.
"""
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent))
from alerts.notifier import Notifier

load_dotenv()

VPS_URL = os.environ["VPS_HEARTBEAT_URL"]
ALERT_AFTER_MIN = int(os.environ.get("HEARTBEAT_ALERT_AFTER_MINUTES", 5))
POLL_INTERVAL_SEC = 60

notifier = Notifier(
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

_already_alerted = False


def check_once():
    global _already_alerted
    try:
        resp = requests.get(VPS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        last_update = data.get("last_state_update")
        stale = False
        if last_update:
            last_dt = datetime.fromisoformat(last_update)
            age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            stale = age_min > ALERT_AFTER_MIN

        if data.get("trading_halted"):
            print(f"[watchdog] VPS reachable, trading halted: {data}")
        elif stale:
            raise TimeoutError(f"State hasn't updated in >{ALERT_AFTER_MIN} min")
        else:
            print(f"[watchdog] OK — {data}")
            _already_alerted = False

    except Exception as e:
        print(f"[watchdog] ALERT: VPS unreachable or stale — {e}")
        if not _already_alerted:
            notifier.notify(
                "heartbeat_missed",
                f"Local watchdog cannot confirm the VPS trading bot is healthy: {e}. "
                f"Check the VPS immediately — open positions may be unmonitored. "
                f"(Exchange-side stop-losses remain active independently of the bot process.)",
                priority="high",
            )
            _already_alerted = True  # don't spam — one alert per outage until it recovers


if __name__ == "__main__":
    print(f"Watchdog started. Polling {VPS_URL} every {POLL_INTERVAL_SEC}s.")
    while True:
        check_once()
        time.sleep(POLL_INTERVAL_SEC)
