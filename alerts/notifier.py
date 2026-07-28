"""
Unified notifier with styled trade logging for Telegram, Discord, and HTML email.

Every trade is logged with rich formatting:
- Telegram: HTML-formatted messages with bold, italic, monospace
- Discord: Embed objects with colors, fields, and footers
- Email: Beautiful HTML templates with logos, colors, and professional layout
- Event log: JSON lines for dashboard consumption

Trade notifications include: symbol, side, amount, entry price, SL/TP, PnL,
running totals, and session statistics.
"""
import smtplib
import json
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# ── Email color palette ──────────────────────────────────────────────────
EMAIL_COLORS = {
    "bg_body":      "#0f1117",
    "bg_card":      "#1a1d2e",
    "bg_header":    "#6c5ce7",
    "bg_footer":    "#12141f",
    "text_primary": "#ffffff",
    "text_secondary":"#a0a0b0",
    "accent_green": "#00d68f",
    "accent_red":   "#ff4757",
    "accent_blue":  "#3b82f6",
    "accent_orange":"#ff9f43",
    "accent_purple":"#a855f7",
    "border":       "#2d2f3e",
}


# ── HTML email builder ───────────────────────────────────────────────────

def _email_header(title: str, subtitle: str = "", color: str = None) -> str:
    color = color or EMAIL_COLORS["bg_header"]
    subtitle_html = f'<p style="margin:4px 0 0;color:#a0a0b0;font-size:13px;">{subtitle}</p>' if subtitle else ""
    return f"""
    <div style="background:{color};padding:28px 32px;border-radius:12px 12px 0 0;text-align:center;">
      <img src="https://img.icons8.com/fluency/48/chart-upward.png" width="40" height="40"
           style="margin-bottom:8px;filter:brightness(0) invert(1);" alt="logo"/>
      <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:0.5px;">{title}</h1>
      {subtitle_html}
    </div>"""


def _email_footer() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
    <div style="background:{EMAIL_COLORS['bg_footer']};padding:18px 32px;border-radius:0 0 12px 12px;text-align:center;border-top:1px solid {EMAIL_COLORS['border']};">
      <img src="https://img.icons8.com/fluency/20/chart-upward.png" width="18" height="18"
           style="vertical-align:middle;margin-right:6px;filter:brightness(0) invert(0.7);" alt="logo"/>
      <span style="color:#6b7280;font-size:12px;">Wamucheha Trading Bot</span>
      <span style="color:#3d3f50;font-size:12px;margin:0 8px;">|</span>
      <span style="color:#6b7280;font-size:12px;">{now}</span>
      <p style="margin:8px 0 0;color:#4b5563;font-size:11px;">
        Automated alerts &mdash; Do not reply directly to this email.
      </p>
    </div>"""


def _kv_row(label: str, value: str, mono: bool = False) -> str:
    font = "font-family:'Courier New',monospace;font-size:13px;" if mono else "font-size:14px;"
    return f"""
    <tr>
      <td style="padding:6px 0;color:{EMAIL_COLORS['text_secondary']};font-size:13px;width:130px;vertical-align:top;">{label}</td>
      <td style="padding:6px 0;color:{EMAIL_COLORS['text_primary']};{font}">{value}</td>
    </tr>"""


def _section_divider(title: str) -> str:
    return f"""
    <tr>
      <td colspan="2" style="padding:16px 0 6px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="border-bottom:1px solid {EMAIL_COLORS['border']};"></td>
          <td style="padding:0 12px;color:{EMAIL_COLORS['accent_purple']};font-size:11px;font-weight:600;letter-spacing:1px;white-space:nowrap;">{title}</td>
          <td style="border-bottom:1px solid {EMAIL_COLORS['border']};"></td>
        </tr></table>
      </td>
    </tr>"""


def _build_email_body(header_html: str, rows_html: str, footer_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:{EMAIL_COLORS['bg_body']};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{EMAIL_COLORS['bg_body']};padding:24px 0;">
<tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="background:{EMAIL_COLORS['bg_card']};border-radius:12px;overflow:hidden;border:1px solid {EMAIL_COLORS['border']};">
  <tr><td>{header_html}</td></tr>
  <tr><td style="padding:24px 28px;">
    <table width="100%" cellpadding="0" cellspacing="0">{rows_html}</table>
  </td></tr>
  <tr><td>{footer_html}</td></tr>
</table>
</td></tr></table>
</body></html>"""


# ── Trade email builders ──────────────────────────────────────────────────

def _build_trade_open_email(symbol, side, amount, entry_price, stop_loss,
                             take_profit, exchange, dry_run, strategies,
                             score, regime, session_stats) -> str:
    mode = "PAPER TRADING" if dry_run else "LIVE"
    is_buy = side == "buy"
    mode_color = "#ff9f43" if dry_run else "#00d68f"
    side_color = EMAIL_COLORS["accent_green"] if is_buy else EMAIL_COLORS["accent_red"]
    side_label = "BUY / LONG" if is_buy else "SELL / SHORT"
    header_color = side_color

    rows = ""
    rows += _kv_row("Mode", f'<span style="color:{mode_color};font-weight:700;">{mode}</span>')
    rows += _kv_row("Pair", f'<span style="color:#fff;font-weight:600;">{symbol}</span>')
    rows += _kv_row("Side", f'<span style="color:{side_color};font-weight:600;">{side_label}</span>')
    rows += _kv_row("Entry Price", entry_price, mono=True)
    rows += _kv_row("Amount", f"{amount:.4f}", mono=True)
    rows += _kv_row("Stop Loss", f'<span style="color:#ff4757;">{stop_loss:.5f}</span>', mono=True)
    rows += _kv_row("Take Profit", f'<span style="color:#00d68f;">{take_profit:.5f}</span>', mono=True)
    rows += _kv_row("Exchange", exchange.upper())

    if strategies:
        rows += _section_divider("STRATEGY DETAILS")
        rows += _kv_row("Strategies", ", ".join(strategies))
    if score:
        rows += _kv_row("Confidence", f"{score:.1%}")
    if regime:
        rows += _kv_row("Market Regime", regime)

    rows += _section_divider("SESSION STATS")
    wins = session_stats.get("wins", 0)
    losses = session_stats.get("losses", 0)
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    total_pnl = session_stats.get("total_pnl", 0)
    total_profit = session_stats.get("total_profit", 0)
    total_loss = session_stats.get("total_loss", 0)
    start_bal = session_stats.get("start_balance", 0)
    total_money = start_bal + total_pnl
    rows += _kv_row("Total Trades", str(session_stats.get("total_trades", 0)))
    rows += _kv_row("Win / Loss", f"{wins} / {losses}")
    rows += _kv_row("Win Rate", f"{wr:.1f}%")
    rows += _kv_row("Total Profit", f'<span style="color:#00d68f;font-weight:600;">${total_profit:+.2f}</span>')
    rows += _kv_row("Total Loss", f'<span style="color:#ff4757;font-weight:600;">${total_loss:+.2f}</span>')
    rows += _kv_row("Session PnL", f'<span style="color:{pnl_color if total_pnl < 0 else "#00d68f"};font-weight:600;">${total_pnl:+.2f}</span>')
    rows += _kv_row("Total Money", f'<span style="color:#fff;font-weight:700;">${total_money:.2f}</span>')

    header = _email_header(
        f"{'🟢' if is_buy else '🔴'} Trade Opened — {symbol}",
        f"{side_label} {amount:.4f} on {exchange.upper()}",
        header_color,
    )
    footer = _email_footer()
    return _build_email_body(header, rows, footer)


def _build_trade_close_email(symbol, side, amount, entry_price, exit_price,
                              pnl, exchange, reason, strategies,
                              session_stats) -> str:
    is_win = pnl > 0
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if side == "buy" \
        else ((entry_price - exit_price) / entry_price * 100)
    result_label = "PROFIT" if is_win else "LOSS"
    result_emoji = "💰" if is_win else "💸"
    header_color = EMAIL_COLORS["accent_green"] if is_win else EMAIL_COLORS["accent_red"]
    pnl_color = EMAIL_COLORS["accent_green"] if is_win else EMAIL_COLORS["accent_red"]

    rows = ""
    rows += _kv_row("Result", f'<span style="color:{pnl_color};font-weight:700;font-size:15px;">{result_emoji} {result_label}</span>')
    rows += _kv_row("Pair", f'<span style="color:#fff;font-weight:600;">{symbol}</span>')
    rows += _kv_row("Side", side.upper())
    rows += _kv_row("Entry Price", entry_price, mono=True)
    rows += _kv_row("Exit Price", exit_price, mono=True)
    rows += _kv_row("PnL", f'<span style="color:{pnl_color};font-weight:700;font-size:15px;">{pnl:+.2f} USD ({pnl_pct:+.2f}%)</span>', mono=True)
    rows += _kv_row("Reason", reason or "N/A")
    rows += _kv_row("Exchange", exchange.upper())

    if strategies:
        rows += _section_divider("STRATEGY DETAILS")
        rows += _kv_row("Strategies", ", ".join(strategies))

    rows += _section_divider("SESSION STATS")
    wins = session_stats.get("wins", 0)
    losses = session_stats.get("losses", 0)
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    total_pnl = session_stats.get("total_pnl", 0)
    total_profit = session_stats.get("total_profit", 0)
    total_loss = session_stats.get("total_loss", 0)
    start_bal = session_stats.get("start_balance", 0)
    total_money = start_bal + total_pnl
    rows += _kv_row("Total Trades", str(session_stats.get("total_trades", 0)))
    rows += _kv_row("Win / Loss", f"{wins} / {losses}")
    rows += _kv_row("Win Rate", f"{wr:.1f}%")
    rows += _kv_row("Total Profit", f'<span style="color:#00d68f;font-weight:600;">${total_profit:+.2f}</span>')
    rows += _kv_row("Total Loss", f'<span style="color:#ff4757;font-weight:600;">${total_loss:+.2f}</span>')
    rows += _kv_row("Session PnL", f'<span style="color:{pnl_color};font-weight:600;">${total_pnl:+.2f}</span>')
    rows += _kv_row("Total Money", f'<span style="color:#fff;font-weight:700;">${total_money:.2f}</span>')
    rows += _kv_row("Best Trade", f"${session_stats.get('best_trade', 0):+.2f}")
    rows += _kv_row("Worst Trade", f"${session_stats.get('worst_trade', 0):+.2f}")

    header = _email_header(
        f"{result_emoji} Trade Closed — {result_label} — {symbol}",
        f"{side.upper()} {amount:.4f} on {exchange.upper()} | {pnl:+.2f} USD",
        header_color,
    )
    footer = _email_footer()
    return _build_email_body(header, rows, footer)


def _build_hourly_summary_email(summary: dict, session_stats: dict) -> str:
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
    header_color = EMAIL_COLORS["accent_green"] if is_profit else EMAIL_COLORS["accent_red"]
    pnl_color = EMAIL_COLORS["accent_green"] if is_profit else EMAIL_COLORS["accent_red"]
    status_color = "#ff4757" if halted else "#00d68f"
    status_label = "HALTED" if halted else "ACTIVE"

    rows = ""
    rows += _kv_row("Status", f'<span style="color:{status_color};font-weight:700;">● {status_label}</span>')
    rows += _kv_row("Time", summary.get("ts", "N/A")[:19] + "Z")
    rows += _section_divider("HOURLY PERFORMANCE")
    rows += _kv_row("Trades", str(trades))
    rows += _kv_row("Wins / Losses", f"{wins} / {losses}")
    rows += _kv_row("Hour PnL", f'<span style="color:{pnl_color};font-weight:600;">{pnl:+.2f} USD</span>')
    rows += _section_divider("ACCOUNT STATUS")
    rows += _kv_row("Balance", f'<span style="color:#fff;font-weight:600;">${balance:.2f}</span>')
    rows += _kv_row("Daily PnL", f'<span style="color:{pnl_color};">{daily_pnl:+.2f} USD</span>')
    rows += _kv_row("Open Positions", str(open_pos))
    rows += _kv_row("Consecutive Losses", str(consecutive))
    rows += _section_divider("SESSION TOTALS")
    s_wins = session_stats.get("wins", 0)
    s_losses = session_stats.get("losses", 0)
    s_total = s_wins + s_losses
    s_wr = (s_wins / s_total * 100) if s_total > 0 else 0
    rows += _kv_row("Total Trades", str(session_stats.get("total_trades", 0)))
    rows += _kv_row("Win Rate", f"{s_wr:.1f}%")
    rows += _kv_row("Session PnL", f'<span style="color:{pnl_color};font-weight:600;">${session_stats.get("total_pnl", 0):+.2f}</span>')

    header = _email_header(
        f"{'📊' if is_profit else '📉'} Hourly Summary Report",
        f"{trades} trades | PnL: {pnl:+.2f} USD | Balance: ${balance:.2f}",
        header_color,
    )
    footer = _email_footer()
    return _build_email_body(header, rows, footer)


def _build_alert_email(event_type: str, message: str, priority: str) -> str:
    priority_colors = {
        "critical": "#ff4757",
        "high":     "#ff9f43",
        "normal":   "#3b82f6",
        "low":      "#6b7280",
    }
    color = priority_colors.get(priority, "#3b82f6")
    priority_label = priority.upper()

    rows = ""
    rows += _kv_row("Priority", f'<span style="color:{color};font-weight:700;">{priority_label}</span>')
    rows += _kv_row("Event", f'<span style="color:#fff;font-weight:600;">{event_type}</span>')
    rows += _section_divider("ALERT DETAILS")
    rows += f"""<tr><td colspan="2" style="padding:12px 0;color:{EMAIL_COLORS['text_primary']};font-size:14px;line-height:1.6;white-space:pre-wrap;">{message}</td></tr>"""

    header = _email_header(
        f"{'🚨' if priority in ('critical','high') else 'ℹ️'} {event_type.replace('_', ' ').title()}",
        f"Priority: {priority_label}",
        color,
    )
    footer = _email_footer()
    return _build_email_body(header, rows, footer)


# ── Subject line builder ──────────────────────────────────────────────────

def _email_subject(event_type: str, trade_data: dict = None, priority: str = "normal") -> str:
    prefix = "🚨" if priority in ("critical", "high") else "📊"
    if event_type == "trade_opened" and trade_data:
        sym = trade_data.get("symbol", "")
        side = trade_data.get("side", "").upper()
        mode = "📝" if trade_data.get("dry_run") else "💰"
        return f"{prefix} {mode} Trade Opened: {side} {sym}"
    if event_type == "trade_closed" and trade_data:
        sym = trade_data.get("symbol", "")
        pnl = trade_data.get("pnl", 0)
        result = "✅ Profit" if pnl > 0 else "❌ Loss"
        return f"{prefix} {result}: {sym} ({pnl:+.2f} USD)"
    if event_type == "hourly_summary":
        return f"{prefix} Hourly Report — {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    if event_type == "heartbeat_missed":
        return f"🚨 CRITICAL: VPS Bot Unreachable — Immediate Action Required"
    if event_type == "circuit_breaker_triggered":
        return f"🚨 CRITICAL: Circuit Breaker Triggered — Trading Halted"
    if event_type == "daily_loss_limit_hit":
        return f"🚨 CRITICAL: Daily Loss Limit Reached — Trading Halted"
    return f"{prefix} [{event_type.replace('_', ' ').title()}] Wamucheha Bot"


# ── Main Notifier class ──────────────────────────────────────────────────

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

        self._session_stats = {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "start_balance": 0.0,
            "total_profit": 0.0, "total_loss": 0.0,
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

        for send_fn in (self._send_telegram, self._send_discord, self._send_email):
            try:
                send_fn(event_type, message, priority, trade_data)
            except Exception as e:
                logger.error(f"Notifier channel failed ({send_fn.__name__}): {e}")

    # ── Trade open ────────────────────────────────────────────────────────

    def notify_trade_opened(self, symbol: str, side: str, amount: float,
                             entry_price: float, stop_loss: float, take_profit: float,
                             exchange: str, dry_run: bool = False, strategies: list = None,
                             score: float = 0, regime: str = ""):
        self._session_stats["total_trades"] += 1

        trade_data = {
            "symbol": symbol, "side": side, "amount": amount,
            "entry_price": entry_price, "stop_loss": stop_loss,
            "take_profit": take_profit, "exchange": exchange,
            "dry_run": dry_run,
        }

        # ── Telegram ──────────────────────────────────────────────────────
        mode = "📝 PAPER" if dry_run else "💰 LIVE"
        emoji = "🟢" if side == "buy" else "🔴"
        side_text = "BUY / LONG" if side == "buy" else "SELL / SHORT"
        tg_msg = (
            f"<b>{emoji} TRADE OPENED [{mode}]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>{symbol}</b>  {'📈' if side == 'buy' else '📉'} {side_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Entry Price:</b>  <code>{entry_price:.5f}</code>\n"
            f"📦 <b>Amount:</b>  <code>{amount:.4f}</code>\n"
            f"🛑 <b>Stop Loss:</b>  <code>{stop_loss:.5f}</code>\n"
            f"🎯 <b>Take Profit:</b>  <code>{take_profit:.5f}</code>\n"
            f"🔄 <b>Exchange:</b>  {exchange.upper()}\n"
        )
        if strategies:
            tg_msg += f"🧠 <b>Strategies:</b>  {', '.join(strategies)}\n"
        if score:
            tg_msg += f"📊 <b>Score:</b>  <code>{score:.3f}</code>\n"
        if regime:
            tg_msg += f"🌐 <b>Regime:</b>  {regime}\n"
        tg_msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        tg_msg += f"<i>📋 Session: {self._session_stats['total_trades']} trades | "
        tg_msg += f"🟢 {self._session_stats['wins']}W / 🔴 {self._session_stats['losses']}L | "
        tg_msg += f"💰 {self._session_stats['total_pnl']:+.2f} USD</i>"
        self._send_telegram_styled(tg_msg)

        # ── Discord ───────────────────────────────────────────────────────
        color = COLOR_GREEN if side == "buy" else COLOR_RED
        fields = [
            {"name": "📊 Pair", "value": f"`{symbol}`", "inline": True},
            {"name": "📈 Side", "value": side_text, "inline": True},
            {"name": "📝 Mode", "value": mode, "inline": True},
            {"name": "💰 Entry", "value": f"`{entry_price:.5f}`", "inline": True},
            {"name": "📦 Amount", "value": f"`{amount:.4f}`", "inline": True},
            {"name": "🔄 Exchange", "value": exchange.upper(), "inline": True},
            {"name": "🛑 Stop Loss", "value": f"`{stop_loss:.5f}`", "inline": True},
            {"name": "🎯 Take Profit", "value": f"`{take_profit:.5f}`", "inline": True},
        ]
        if strategies:
            fields.append({"name": "🧠 Strategies", "value": ", ".join(strategies), "inline": False})
        if score:
            fields.append({"name": "📊 Score", "value": f"`{score:.3f}`", "inline": True})
        if regime:
            fields.append({"name": "🌐 Regime", "value": regime, "inline": True})
        self._send_discord_embed(
            title=f"{emoji} Trade Opened [{mode}] — {symbol}",
            description=f"**{side_text}** {amount:.4f} of `{symbol}` on {exchange.upper()}",
            color=color,
            fields=fields,
            footer=f"Session: {self._session_stats['total_trades']} trades | W/L: {self._session_stats['wins']}/{self._session_stats['losses']} | PnL: {self._session_stats['total_pnl']:+.2f} USD",
        )

        # ── Email ─────────────────────────────────────────────────────────
        self._send_email("trade_opened", trade_data=trade_data, priority="normal")

        # ── Event log ─────────────────────────────────────────────────────
        self._log_trade("opened", trade_data)

    # ── Trade close ───────────────────────────────────────────────────────

    def notify_trade_closed(self, symbol: str, side: str, amount: float,
                             entry_price: float, exit_price: float, pnl: float,
                             exchange: str, reason: str = "",
                             strategies: list = None):
        is_win = pnl > 0
        if is_win:
            self._session_stats["wins"] += 1
            self._session_stats["total_profit"] += pnl
        else:
            self._session_stats["losses"] += 1
            self._session_stats["total_loss"] += pnl
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

        # ── Telegram ──────────────────────────────────────────────────────
        s = self._session_stats
        wr = s['wins'] / max(1, s['wins'] + s['losses']) * 100
        total_money = s['start_balance'] + s['total_pnl']
        tg_msg = (
            f"<b>{emoji} TRADE CLOSED [{result_text}]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>{symbol}</b>  {'📈' if side == 'buy' else '📉'} {side.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Entry:</b>  <code>{entry_price:.5f}</code>\n"
            f"💰 <b>Exit:</b>  <code>{exit_price:.5f}</code>\n"
            f"📦 <b>Amount:</b>  <code>{amount:.4f}</code>\n"
            f"🔄 <b>Exchange:</b>  {exchange.upper()}\n"
            f"📝 <b>Reason:</b>  {reason or 'N/A'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_emoji} <b>PnL:</b>  <code>{pnl:+.2f} USD ({pnl_pct:+.2f}%)</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Session Stats</b>\n"
            f"  🟢 Wins: <code>{s['wins']}</code>  |  🔴 Losses: <code>{s['losses']}</code>\n"
            f"  📊 Win Rate: <code>{wr:.1f}%</code>\n"
            f"  💵 Total Profit: <code>${s['total_profit']:+.2f}</code>\n"
            f"  📉 Total Loss: <code>${s['total_loss']:+.2f}</code>\n"
            f"  💰 Total PnL: <code>{s['total_pnl']:+.2f} USD</code>\n"
            f"  🏦 Total Money: <code>${total_money:.2f}</code>\n"
            f"  🏆 Best: <code>{s['best_trade']:+.2f}</code>  |  💀 Worst: <code>{s['worst_trade']:+.2f}</code>"
        )
        self._send_telegram_styled(tg_msg)

        # ── Discord ───────────────────────────────────────────────────────
        s = self._session_stats
        wr = s['wins'] / max(1, s['wins'] + s['losses']) * 100
        total_money = s['start_balance'] + s['total_pnl']
        color = COLOR_GREEN if is_win else COLOR_RED
        fields = [
            {"name": "📊 Pair", "value": f"`{symbol}`", "inline": True},
            {"name": "📈 Side", "value": side.upper(), "inline": True},
            {"name": "🎯 Result", "value": f"{'✅' if is_win else '❌'} {result_text}", "inline": True},
            {"name": "💰 Entry", "value": f"`{entry_price:.5f}`", "inline": True},
            {"name": "💰 Exit", "value": f"`{exit_price:.5f}`", "inline": True},
            {"name": "📦 Amount", "value": f"`{amount:.4f}`", "inline": True},
            {"name": "📝 Reason", "value": reason or "N/A", "inline": True},
            {"name": "🔄 Exchange", "value": exchange.upper(), "inline": True},
            {"name": f"{pnl_emoji} PnL", "value": f"`{pnl:+.2f} USD ({pnl_pct:+.2f}%)`", "inline": True},
            {"name": "━━━━ Session Stats ━━━━", "value": "━━━━━━━━━━━━━━━━━", "inline": False},
            {"name": "🟢 Wins", "value": f"`{s['wins']}`", "inline": True},
            {"name": "🔴 Losses", "value": f"`{s['losses']}`", "inline": True},
            {"name": "📊 Win Rate", "value": f"`{wr:.1f}%`", "inline": True},
            {"name": "💵 Total Profit", "value": f"`${s['total_profit']:+.2f}`", "inline": True},
            {"name": "📉 Total Loss", "value": f"`${s['total_loss']:+.2f}`", "inline": True},
            {"name": "💰 Total PnL", "value": f"`{s['total_pnl']:+.2f} USD`", "inline": True},
            {"name": "🏦 Total Money", "value": f"`${total_money:.2f}`", "inline": True},
            {"name": "🏆 Best Trade", "value": f"`{s['best_trade']:+.2f}`", "inline": True},
            {"name": "💀 Worst Trade", "value": f"`{s['worst_trade']:+.2f}`", "inline": True},
        ]
        if strategies:
            fields.append({"name": "🧠 Strategies Used", "value": ", ".join(strategies), "inline": False})
        self._send_discord_embed(
            title=f"{emoji} Trade Closed [{result_text}] — {symbol}",
            description=f"**{side.upper()}** {amount:.4f} of `{symbol}` — `{pnl:+.2f} USD` ({pnl_pct:+.2f}%)",
            color=color,
            fields=fields,
            footer=f"Total: {s['total_trades']} trades | Profit: ${s['total_profit']:+.2f} | Loss: ${s['total_loss']:+.2f} | PnL: {s['total_pnl']:+.2f} USD",
        )

        # ── Email ─────────────────────────────────────────────────────────
        self._send_email("trade_closed", trade_data=trade_data, priority="normal")

        # ── Event log ─────────────────────────────────────────────────────
        self._log_trade("closed", trade_data)

    # ── Hourly summary ────────────────────────────────────────────────────

    def notify_hourly_summary(self, summary: dict):
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

        # ── Telegram ──────────────────────────────────────────────────────
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

        # ── Discord ───────────────────────────────────────────────────────
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

        # ── Email ─────────────────────────────────────────────────────────
        self._send_email("hourly_summary", trade_data=summary, priority="normal")

        # ── Event log ─────────────────────────────────────────────────────
        payload = {
            "type": "hourly_summary",
            "message": f"Hourly: {trades} trades, PnL: {pnl:+.2f}, Balance: ${balance:.2f}",
            "priority": "normal",
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_data": summary,
        }
        self._log(payload)

    # ── Internal send methods ─────────────────────────────────────────────

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
        if event_type in ("trade_opened", "trade_closed", "hourly_summary"):
            return  # handled by dedicated methods
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
            return  # handled by dedicated methods
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

    def _send_email(self, event_type: str, message: str = "",
                     priority: str = "normal", trade_data: dict = None):
        cfg = self.email_cfg
        if not cfg.get("address") or not cfg.get("to"):
            return

        # Build HTML body based on event type
        if event_type == "trade_opened" and trade_data:
            html = _build_trade_open_email(
                symbol=trade_data.get("symbol", ""),
                side=trade_data.get("side", ""),
                amount=trade_data.get("amount", 0),
                entry_price=trade_data.get("entry_price", 0),
                stop_loss=trade_data.get("stop_loss", 0),
                take_profit=trade_data.get("take_profit", 0),
                exchange=trade_data.get("exchange", ""),
                dry_run=trade_data.get("dry_run", False),
                strategies=trade_data.get("strategies"),
                score=trade_data.get("score", 0),
                regime=trade_data.get("regime", ""),
                session_stats=self._session_stats,
            )
        elif event_type == "trade_closed" and trade_data:
            html = _build_trade_close_email(
                symbol=trade_data.get("symbol", ""),
                side=trade_data.get("side", ""),
                amount=trade_data.get("amount", 0),
                entry_price=trade_data.get("entry_price", 0),
                exit_price=trade_data.get("exit_price", 0),
                pnl=trade_data.get("pnl", 0),
                exchange=trade_data.get("exchange", ""),
                reason=trade_data.get("reason", ""),
                strategies=trade_data.get("strategies"),
                session_stats=self._session_stats,
            )
        elif event_type == "hourly_summary" and trade_data:
            html = _build_hourly_summary_email(trade_data, self._session_stats)
        else:
            html = _build_alert_email(event_type, message or trade_data.get("message", "") if trade_data else "", priority)

        subject = _email_subject(event_type, trade_data, priority)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Wamucheha Bot <{cfg['address']}>"
        msg["To"] = cfg["to"]
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["address"], cfg["app_password"])
                server.send_message(msg)
            logger.info(f"Email sent: {subject}")
        except Exception as e:
            logger.error(f"Email send failed: {e}")

    def get_session_stats(self) -> dict:
        return self._session_stats.copy()
