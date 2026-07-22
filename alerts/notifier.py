"""
Unified notifier. Fires every alert to ALL enabled channels — never rely on a single
channel, since Telegram/Discord/SMTP can each independently go down or rate-limit.
Also writes every alert to the local event log, which the dashboard reads.
"""
import smtplib
import json
import logging
from email.mime.text import MIMEText
from datetime import datetime, timezone
from pathlib import Path

import requests
from discord_webhook import DiscordWebhook

logger = logging.getLogger("notifier")
EVENT_LOG = Path(__file__).parent.parent / "data" / "events.log"

HIGH_PRIORITY_EVENTS = {"circuit_breaker_triggered", "daily_loss_limit_hit", "heartbeat_missed"}


class Notifier:
    def __init__(self, telegram_token=None, telegram_chat_id=None,
                 discord_webhook_url=None, email_cfg=None):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = discord_webhook_url
        self.email_cfg = email_cfg or {}
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)

    def notify(self, event_type: str, message: str, priority: str = "normal"):
        payload = {
            "type": event_type,
            "message": message,
            "priority": priority,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._log(payload)

        # Fire to every channel independently — one failing must not block the others
        for send_fn in (self._send_telegram, self._send_discord, self._send_email):
            try:
                send_fn(event_type, message, priority)
            except Exception as e:
                logger.error(f"Notifier channel failed ({send_fn.__name__}): {e}")

    def _log(self, payload: dict):
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def _send_telegram(self, event_type, message, priority):
        if not (self.telegram_token and self.telegram_chat_id):
            return
        prefix = "🚨 " if priority == "high" else "ℹ️ "
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        requests.post(url, json={
            "chat_id": self.telegram_chat_id,
            "text": f"{prefix}[{event_type}] {message}",
        }, timeout=10)

    def _send_discord(self, event_type, message, priority):
        if not self.discord_webhook_url:
            return
        prefix = "🚨 " if priority == "high" else "ℹ️ "
        DiscordWebhook(url=self.discord_webhook_url, content=f"{prefix}**{event_type}**: {message}").execute()

    def _send_email(self, event_type, message, priority):
        # Only email for high-priority events by default — avoid inbox fatigue for routine trades
        if priority != "high" and event_type not in HIGH_PRIORITY_EVENTS:
            return
        cfg = self.email_cfg
        if not cfg.get("address"):
            return
        msg = MIMEText(message)
        msg["Subject"] = f"[Trading Bot] {event_type}"
        msg["From"] = cfg["address"]
        msg["To"] = cfg["to"]
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["address"], cfg["app_password"])
            server.send_message(msg)
