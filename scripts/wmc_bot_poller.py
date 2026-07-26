"""
WMC-Bot (@WamuchehaBot) — Telegram getUpdates poller
Polls Telegram's getUpdates endpoint in a loop, tracks the offset so you
don't reprocess old updates, and gives you a single place (handle_update)
to plug in your own logic.
Usage:
    export WMC_BOT_TOKEN="your-token-here"
    python3 scripts/wmc_bot_poller.py
"""
import os
import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wmc-bot")

BOT_TOKEN = os.environ.get("WMC_BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_TIMEOUT = 30
POLL_INTERVAL = 1


def get_updates(offset=None):
    params = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=POLL_TIMEOUT + 10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.error("Network error calling getUpdates: %s", e)
        return []
    except ValueError:
        log.error("Non-JSON response from Telegram: %s", resp.text[:200])
        return []
    if not data.get("ok"):
        log.error("Telegram API error: %s", data)
        return []
    return data.get("result", [])


def handle_update(update: dict):
    message = update.get("message")
    if not message:
        log.info("Received non-message update: %s", list(update.keys()))
        return
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    from_user = message.get("from", {}).get("username", "unknown")
    log.info("Message from @%s (chat_id=%s): %s", from_user, chat_id, text)
    # send_message(chat_id, f"Got it: {text}")


def send_message(chat_id, text):
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send message: %s", e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set WMC_BOT_TOKEN in your environment before running.")
    log.info("Starting WMC-Bot poller...")
    last_update_id = None
    while True:
        updates = get_updates(offset=last_update_id)
        for update in updates:
            handle_update(update)
            last_update_id = update["update_id"] + 1
        if not updates:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
