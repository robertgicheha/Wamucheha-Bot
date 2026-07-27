"""
Unified notifier with styled trade logging for Telegram and Discord.

Every trade is logged with rich formatting:
- Telegram: HTML-formatted messages with bold, italic, monospace
- Discord: Embed objects with colors, fields, and footers
- Email: Only high-priority alerts
- Event log: JSON lines for dashboard consumption

Trade notifications include: symbol, side, amount, entry price, SL/TP, PnL,
running totals, and session statistics.
"""
import smtplib
import json
import logging
from email.mime.text import MIMEText
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger("notifier")
EVENT_LOG = Path(__file__).parent.parent / "data" / "events.log"
TRADE_LOG = Path(__file__).parent.parent / "data" / "trade_log.jsonl"

HIGH_PRIORITY_EVENTS = {"circuit_breaker_triggered", "daily_loss_limit_hit", "heartbeat_missed"}

# Color codes for Discord embeds
COLOR_GREEN = 0x4CAF50
COLOR_RED = 0xF44336
COLOR_BLUE = 0x2196F3
COLOR_ORANGE = 0xFF9800
COLOR_YELLOW = 0xFFEB3B
COLOR_PURPLE = 0x9C27B0
COLOR_CYAN = 0x00BCD4
COLOR_GRAY = 0x9E9E9E
COLOR_DARK = 0x1A1A2E


class Notifier:
    def __init__(self, telegram_token=None, telegram_chat_id=None,
                 discord_webhook_url=None, email_cfg=None,
                 discord_webhook_trades=None):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = discord_webhook_url
        self.discord_webhook_trades = discord_webhook_trades or discord_webhook_url
        self.email_cfg = email_cfg or {}
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)

        # Running session stats for richer notifications
        self._session_stats = {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "start_balance": 0.0,
        }

    def update_start_balance(self, balance: float):
        self._session_stats["start_balance"] = balance

    def notify(self, event_type: str, message: str, priority: str = "normal",
               trade_data: dict = None):
        payload = {
            "type": event_type,
            "message": message,
            "priority": priority,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if trade_data:
            payload["trade_data"] = trade_data
        self._log(payload)

        # Fire to every channel independently
        for send_fn in (self._send_telegram, self._send_discord, self._send_email):
            try:
                send_fn(event_type, message, priority, trade_data)
            except Exception as e:
                logger.error(f"Notifier channel failed ({send_fn.__name__}): {e}")

    def notify_trade_opened(self, symbol: str, side: str, amount: float,
                             entry_price: float, stop_loss: float, take_profit: float,
                             exchange: str, dry_run: bool = False, strategies: list = None,
                             score: float = 0, regime: str = ""):
        """Styled notification for trade open."""
        self._session_stats["total_trades"] += 1

        trade_data = {
            "symbol": symbol, "side": side, "amount": amount,
            "entry_price": entry_price, "stop_loss": stop_loss,
            "take_profit": take_profit, "exchange": exchange,
        }

        mode = "DRY-RUN" if dry_run else "LIVE"
        emoji = "🟢" if side == "buy" else "🔴"
        side_text = "BUY / LONG" if side == "buy" else "SELL / SHORT"

        # Telegram HTML
        tg_msg = (
            f"<b>{emoji} TRADE OPENED [{mode}]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Pair:</b>  <code>{symbol}</code>\n"
            f"<b>Side:</b>  {side_text}\n"
            f"<b>Entry:</b>  <code>{entry_price:.5f}</code>\n"
            f"<b>Amount:</b>  <code>{amount:.4f}</code>\n"
            f"<b>Stop Loss:</b>  <code>{stop_loss:.5f}</code>\n"
            f"<b>Take Profit:</b>  <code>{take_profit:.5f}</code>\n"
            f"<b>Exchange:</b>  {exchange.upper()}\n"
        )
        if strategies:
            tg_msg += f"<b>Strategies:</b>  {', '.join(strategies)}\n"
        if score:
            tg_msg += f"<b>Score:</b>  <code>{score:.3f}</code>\n"
        if regime:
            tg_msg += f"<b>Regime:</b>  {regime}\n"
        tg_msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
        tg_msg += f"<i>Session: {self._session_stats['total_trades']} trades | "
        tg_msg += f"W/L: {self._session_stats['wins']}/{self._session_stats['losses']}</i>"

        self._send_telegram_styled(tg_msg)

        # Discord embed
        color = COLOR_GREEN if side == "buy" else COLOR_RED
        fields = [
            {"name": "Pair", "value": f"`{symbol}`", "inline": True},
            {"name": "Side", "value": side_text, "inline": True},
            {"name": "Entry", "value": f"`{entry_price:.5f}`", "inline": True},
            {"name": "Amount", "value": f"`{amount:.4f}`", "inline": True},
            {"name": "Stop Loss", "value": f"`{stop_loss:.5f}`", "inline": True},
            {"name": "Take Profit", "value": f"`{take_profit:.5f}`", "inline": True},
            {"name": "Exchange", "value": exchange.upper(), "inline": True},
            {"name": "Mode", "value": mode, "inline": True},
        ]
        if strategies:
            fields.append({"name": "Strategies", "value": ", ".join(strategies), "inline": False})
        if score:
            fields.append({"name": "Score", "value": f"`{score:.3f}`", "inline": True})
        if regime:
            fields.append({"name": "Regime", "value": regime, "inline": True})

        self._send_discord_embed(
            title=f"{emoji} Trade Opened [{mode}]",
            description=f"**{side_text}** {amount:.4f} of `{symbol}`",
            color=color,
            fields=fields,
            footer=f"Session: {self._session_stats['total_trades']} trades | W/L: {self._session_stats['wins']}/{self._session_stats['losses']}",
        )

        # Log to trade log file
        self._log_trade("opened", trade_data)

    def notify_trade_closed(self, symbol: str, side: str, amount: float,
                             entry_price: float, exit_price: float, pnl: float,
                             exchange: str, reason: str = "",
                             strategies: list = None):
        """Styled notification for trade close with PnL."""
        is_win = pnl > 0
        if is_win:
            self._session_stats["wins"] += 1
        else:
            self._session_stats["losses"] += 1
        self._session_stats["total_pnl"] += pnl
        self._session_stats["best_trade"] = max(self._session_stats["best_trade"], pnl)
        self._session_stats["worst_trade"] = min(self._session_stats["worst_trade"], pnl)

        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if side == "buy" \
            else ((entry_price - exit_price) / entry_price * 100)

        trade_data = {
            "symbol": symbol, "side": side, "amount": amount,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl": pnl, "pnl_pct": pnl_pct, "exchange": exchange,
            "reason": reason,
        }

        emoji = "💰" if is_win else "💸"
        pnl_emoji = "✅" if is_win else "❌"
        result_text = "PROFIT" if is_win else "LOSS"

        # Telegram HTML
        tg_msg = (
            f"<b>{emoji} TRADE CLOSED [{result_text}]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Pair:</b>  <code>{symbol}</code>\n"
            f"<b>Side:</b>  {'BUY' if side == 'buy' else 'SELL'}\n"
            f"<b>Entry:</b>  <code>{entry_price:.5f}</code>\n"
            f"<b>Exit:</b>  <code>{exit_price:.5f}</code>\n"
            f"<b>{pnl_emoji} PnL:</b>  <code>{pnl:+.2f} USD ({pnl_pct:+.2f}%)</code>\n"
            f"<b>Reason:</b>  {reason}\n"
            f"<b>Exchange:</b>  {exchange.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Session Stats:</b>\n"
            f"  Wins: {self._session_stats['wins']} | Losses: {self._session_stats['losses']}\n"
            f"  Win Rate: {self._session_stats['wins'] / max(1, self._session_stats['wins'] + self._session_stats['losses']) * 100:.1f}%\n"
            f"  Total PnL: <code>{self._session_stats['total_pnl']:+.2f} USD</code>\n"
            f"  Best: <code>{self._session_stats['best_trade']:+.2f}</code> | "
            f"Worst: <code>{self._session_stats['worst_trade']:+.2f}</code>"
        )
        self._send_telegram_styled(tg_msg)

        # Discord embed
        color = COLOR_GREEN if is_win else COLOR_RED
        fields = [
            {"name": "Pair", "value": f"`{symbol}`", "inline": True},
            {"name": "Side", "value": side.upper(), "inline": True},
            {"name": "Result", "value": result_text, "inline": True},
            {"name": "Entry", "value": f"`{entry_price:.5f}`", "inline": True},
            {"name": "Exit", "value": f"`{exit_price:.5f}`", "inline": True},
            {"name": "PnL", "value": f"`{pnl:+.2f} USD ({pnl_pct:+.2f}%)`", "inline": True},
            {"name": "Reason", "value": reason or "N/A", "inline": True},
            {"name": "Exchange", "value": exchange.upper(), "inline": True},
            {"name": "── Session Stats ──", "value": "─────────────────", "inline": False},
            {"name": "Win/Loss", "value": f"{self._session_stats['wins']}/{self._session_stats['losses']}", "inline": True},
            {"name": "Win Rate", "value": f"{self._session_stats['wins'] / max(1, self._session_stats['wins'] + self._session_stats['losses']) * 100:.1f}%", "inline": True},
            {"name": "Total PnL", "value": f"`{self._session_stats['total_pnl']:+.2f} USD`", "inline": True},
            {"name": "Best Trade", "value": f"`{self._session_stats['best_trade']:+.2f}`", "inline": True},
            {"name": "Worst Trade", "value": f"`{self._session_stats['worst_trade']:+.2f}`", "inline": True},
        ]
        if strategies:
            fields.append({"name": "Strategies Used", "value": ", ".join(strategies), "inline": False})

        self._send_discord_embed(
            title=f"{emoji} Trade Closed [{result_text}]",
            description=f"**{side.upper()}** {amount:.4f} of `{symbol}` — `{pnl:+.2f} USD`",
            color=color,
            fields=fields,
            footer=f"Session: {self._session_stats['total_trades']} trades | Best: {self._session_stats['best_trade']:+.2f} | Worst: {self._session_stats['worst_trade']:+.2f}",
        )

        # Log to trade log file
        self._log_trade("closed", trade_data)

    def notify_hourly_summary(self, summary: dict):
        """Styled hourly summary report for Telegram and Discord."""
        trades = summary.get("trades_this_hour", 0)
        wins = summary.get("wins_this_hour", 0)
        losses = summary.get("losses_this_hour", 0)
        pnl = summary.get("hour_pnl", 0.0)
        balance = summary.get("trading_balance", 0.0)
        daily_pnl = summary.get("daily_pnl", 0.0)
        open_pos = summary.get("open_positions", 0)
        consecutive = summary.get("consecutive_losses", 0)
        halted = summary.get("trading_halted", False)

        is_profit = pnl >= 0
        emoji = "📊" if is_profit else "📉"

        # Telegram HTML
        tg_msg = (
            f"<b>{emoji} HOURLY SUMMARY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Time:</b>  {summary.get('ts', 'N/A')[:19]}Z\n\n"
            f"<b>Trades This Hour:</b>  {trades}\n"
            f"  Wins: <code>{wins}</code> | Losses: <code>{losses}</code>\n"
            f"  Hour PnL:  <code>{pnl:+.2f} USD</code>\n\n"
            f"<b>Account:</b>\n"
            f"  Balance:  <code>${balance:.2f}</code>\n"
            f"  Daily PnL:  <code>{daily_pnl:+.2f} USD</code>\n"
            f"  Open Positions:  {open_pos}\n"
            f"  Consecutive Losses:  {consecutive}\n\n"
            f"<b>Status:</b>  {'🔴 HALTED' if halted else '🟢 ACTIVE'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Total Session: {self._session_stats['total_trades']} trades | "
            f"PnL: {self._session_stats['total_pnl']:+.2f} USD</i>"
        )
        self._send_telegram_styled(tg_msg)

        # Discord embed
        color = COLOR_GREEN if is_profit else COLOR_RED
        fields = [
            {"name": "Trades", "value": str(trades), "inline": True},
            {"name": "Wins", "value": str(wins), "inline": True},
            {"name": "Losses", "value": str(losses), "inline": True},
            {"name": "Hour PnL", "value": f"`{pnl:+.2f} USD`", "inline": True},
            {"name": "Balance", "value": f"`${balance:.2f}`", "inline": True},
            {"name": "Daily PnL", "value": f"`{daily_pnl:+.2f} USD`", "inline": True},
            {"name": "Open Positions", "value": str(open_pos), "inline": True},
            {"name": "Consecutive Losses", "value": str(consecutive), "inline": True},
            {"name": "Status", "value": "🔴 HALTED" if halted else "🟢 ACTIVE", "inline": True},
        ]

        self._send_discord_embed(
            title=f"{emoji} Hourly Summary",
            description=f"Trading session report for the last hour",
            color=color,
            fields=fields,
            footer=f"Total Session: {self._session_stats['total_trades']} trades | PnL: {self._session_stats['total_pnl']:+.2f} USD",
            webhook_url=self.discord_webhook_trades,
        )

        # Also log the summary
        payload = {
            "type": "hourly_summary",
            "message": f"Hourly: {trades} trades, PnL: {pnl:+.2f}, Balance: ${balance:.2f}",
            "priority": "normal",
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_data": summary,
        }
        self._log(payload)

    # ---------- Internal send methods ----------

    def _log(self, payload: dict):
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    def _log_trade(self, action: str, trade_data: dict):
        entry = {
            "action": action,
            "ts": datetime.now(timezone.utc).isoformat(),
            **trade_data,
        }
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def _send_telegram(self, event_type, message, priority, trade_data=None):
        if not (self.telegram_token and self.telegram_chat_id):
            return
        # Use styled version for trades
        if event_type in ("trade_opened", "trade_closed", "hourly_summary"):
            return  # Already handled by dedicated methods
        prefix = "🚨 " if priority == "high" else "ℹ️ "
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        requests.post(url, json={
            "chat_id": self.telegram_chat_id,
            "text": f"{prefix}<b>[{event_type}]</b>\n{message}",
            "parse_mode": "HTML",
        }, timeout=10)

    def _send_telegram_styled(self, html_message: str):
        if not (self.telegram_token and self.telegram_chat_id):
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        requests.post(url, json={
            "chat_id": self.telegram_chat_id,
            "text": html_message,
            "parse_mode": "HTML",
        }, timeout=10)

    def _send_discord(self, event_type, message, priority, trade_data=None):
        if not self.discord_webhook_url:
            return
        if event_type in ("trade_opened", "trade_closed", "hourly_summary"):
            return  # Already handled by dedicated methods
        prefix = "🚨 " if priority == "high" else "ℹ️ "
        try:
            from discord_webhook import DiscordWebhook
            DiscordWebhook(
                url=self.discord_webhook_url,
                content=f"{prefix}**{event_type}**: {message}",
            ).execute()
        except ImportError:
            pass

    def _send_discord_embed(self, title: str, description: str = "",
                             color: int = COLOR_BLUE, fields: list = None,
                             footer: str = "", webhook_url: str = None):
        url = webhook_url or self.discord_webhook_url
        if not url:
            return
        try:
            from discord_webhook import DiscordWebhook, DiscordEmbed
            webhook = DiscordWebhook(url=url)
            embed = DiscordEmbed(title=title, description=description, color=color)
            if fields:
                for field in fields:
                    embed.add_embed_field(
                        name=field["name"],
                        value=field["value"],
                        inline=field.get("inline", True),
                    )
            if footer:
                embed.set_footer(text=footer)
            embed.set_timestamp(datetime.now(timezone.utc).isoformat())
            webhook.add_embed(embed)
            webhook.execute()
        except ImportError:
            logger.warning("discord-webhook not installed, skipping Discord embed")

    def _send_email(self, event_type, message, priority, trade_data=None):
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

    def get_session_stats(self) -> dict:
        return self._session_stats.copy()
