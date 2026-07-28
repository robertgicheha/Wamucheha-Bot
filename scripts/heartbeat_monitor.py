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
import socket
from pathlib import Path
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent))
from alerts.notifier import Notifier

load_dotenv()

VPS_URL = os.environ["VPS_HEARTBEAT_URL"]
ALERT_AFTER_MIN = int(os.environ.get("HEARTBEAT_ALERT_AFTER_MINUTES", 5))
POLL_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_POLL_INTERVAL_SEC", 60))
CONNECT_TIMEOUT = int(os.environ.get("HEARTBEAT_CONNECT_TIMEOUT_SEC", 15))

# Parse VPS host for quick reachability check
from urllib.parse import urlparse
_vps_host = urlparse(VPS_URL).hostname
_vps_port = urlparse(VPS_URL).port or 80

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
_consecutive_failures = 0


def _check_tcp_reachable(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Quick TCP reachability check before HTTP — identifies network vs app issues."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True, "TCP_OK"
    except socket.timeout:
        return False, f"TCP_TIMEOUT ({host}:{port} unreachable after {timeout}s)"
    except ConnectionRefusedError:
        return False, f"TCP_REFUSED (port {port} closed — service may be down)"
    except OSError as e:
        return False, f"TCP_ERROR ({e})"


def check_once():
    global _already_alerted, _consecutive_failures
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Step 1: Quick TCP check to distinguish network issues from app issues
    tcp_ok, tcp_msg = _check_tcp_reachable(_vps_host, _vps_port, timeout=5)

    if not tcp_ok:
        _consecutive_failures += 1
        print(f"[watchdog] {ts} ALERT ({_consecutive_failures}x): {tcp_msg}")
        if not _already_alerted:
            notifier.notify(
                "heartbeat_missed",
                f"VPS unreachable at network level: {tcp_msg}\n\n"
                f"The watchdog cannot even establish a TCP connection to {_vps_host}:{_vps_port}. "
                f"This typically means:\n"
                f"  1. VPS is powered off or crashed\n"
                f"  2. Firewall is blocking port {_vps_port}\n"
                f"  3. Network route is down\n\n"
                f"Consecutive failures: {_consecutive_failures}\n"
                f"Last successful check: {ts}\n\n"
                f"Exchange-side stop-losses remain active independently of the bot process.",
                priority="high",
            )
            _already_alerted = True
        return

    # Step 2: TCP is open — try the HTTP health endpoint
    try:
        resp = requests.get(VPS_URL, timeout=CONNECT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        last_update = data.get("last_state_update")
        stale = False
        age_min = None
        if last_update:
            last_dt = datetime.fromisoformat(last_update)
            age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            stale = age_min > ALERT_AFTER_MIN

        if data.get("trading_halted"):
            halt_reason = data.get("halt_reason", "unknown")
            print(f"[watchdog] {ts} WARNING: VPS reachable but trading HALTED — {halt_reason}")
            if not _already_alerted:
                notifier.notify(
                    "heartbeat_missed",
                    f"VPS is reachable but trading is HALTED.\n"
                    f"Reason: {halt_reason}\n"
                    f"Uptime: {data.get('uptime_seconds', 0) / 3600:.1f} hours\n\n"
                    f"Open positions may be unmonitored. Check the VPS dashboard immediately.",
                    priority="high",
                )
                _already_alerted = True
        elif stale:
            print(f"[watchdog] {ts} WARNING: State stale for {age_min:.0f} min (threshold: {ALERT_AFTER_MIN} min)")
            if not _already_alerted:
                notifier.notify(
                    "heartbeat_missed",
                    f"VPS bot process may be frozen or unresponsive.\n\n"
                    f"The /health endpoint responds but the last state update was "
                    f"{age_min:.0f} minutes ago (threshold: {ALERT_AFTER_MIN} min).\n"
                    f"This usually means the bot process is stuck or crashed while "
                    f"the FastAPI server keeps running.\n\n"
                    f"Uptime: {data.get('uptime_seconds', 0) / 3600:.1f} hours\n"
                    f"Trading halted: {data.get('trading_halted', False)}\n\n"
                    f"Exchange-side stop-losses remain active independently of the bot process.",
                    priority="high",
                )
                _already_alerted = True
        else:
            print(f"[watchdog] {ts} OK — uptime {data.get('uptime_seconds', 0) / 3600:.1f}h, "
                  f"halted={data.get('trading_halted', False)}, "
                  f"age={age_min:.0f}min" if age_min else f"[watchdog] {ts} OK — {data}")
            _already_alerted = False
            _consecutive_failures = 0

    except requests.Timeout:
        _consecutive_failures += 1
        print(f"[watchdog] {ts} ALERT ({_consecutive_failures}x): HTTP timeout after {CONNECT_TIMEOUT}s")
        if not _already_alerted:
            notifier.notify(
                "heartbeat_missed",
                f"VPS HTTP health endpoint timed out after {CONNECT_TIMEOUT}s.\n\n"
                f"TCP connection to {_vps_host}:{_vps_port} succeeds, but the "
                f"FastAPI server is not responding. The bot process may have crashed "
                f"while the web server remains running.\n\n"
                f"Consecutive failures: {_consecutive_failures}\n\n"
                f"Exchange-side stop-losses remain active independently of the bot process.",
                priority="high",
            )
            _already_alerted = True
    except requests.ConnectionError as e:
        _consecutive_failures += 1
        print(f"[watchdog] {ts} ALERT ({_consecutive_failures}x): {e}")
        if not _already_alerted:
            notifier.notify(
                "heartbeat_missed",
                f"VPS health endpoint connection error: {e}\n\n"
                f"Consecutive failures: {_consecutive_failures}\n\n"
                f"Exchange-side stop-losses remain active independently of the bot process.",
                priority="high",
            )
            _already_alerted = True
    except Exception as e:
        _consecutive_failures += 1
        print(f"[watchdog] {ts} ALERT ({_consecutive_failures}x): {e}")
        if not _already_alerted:
            notifier.notify(
                "heartbeat_missed",
                f"VPS health check failed: {e}\n\n"
                f"Consecutive failures: {_consecutive_failures}\n\n"
                f"Check the VPS immediately — open positions may be unmonitored.\n"
                f"Exchange-side stop-losses remain active independently of the bot process.",
                priority="high",
            )
            _already_alerted = True


if __name__ == "__main__":
    print(f"Watchdog started. Polling {VPS_URL} every {POLL_INTERVAL_SEC}s.")
    print(f"TCP check target: {_vps_host}:{_vps_port} | HTTP timeout: {CONNECT_TIMEOUT}s")
    print(f"Alert threshold: state stale > {ALERT_AFTER_MIN} min")
    while True:
        check_once()
        time.sleep(POLL_INTERVAL_SEC)
