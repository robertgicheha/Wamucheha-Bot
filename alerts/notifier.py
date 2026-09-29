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
import re
import html as html_lib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from pathlib import Path

import requests

from alerts import formatting as f

logger = logging.getLogger("notifier")
EVENT_LOG = Path(__file__).parent.parent / "data" / "events.log"
TRADE_LOG = Path(__file__).parent.parent / "data" / "trade_log.jsonl"

HIGH_PRIORITY_EVENTS = {"circuit_breaker_triggered", "daily_loss_limit_hit", "heartbeat_missed"}

# ── Channel routing ──────────────────────────────────────────────────────
# Email is a scarce, slow-reading channel: it is for the one report that
# stands alone and stays useful after you close it. Everything else is
# real-time chatter that belongs on Telegram/Discord, where it is already
# formatted better and costs nothing. Anything not listed here never
# reaches SMTP — add an event type here if you ever want it emailed.
EMAIL_ALLOWED_EVENTS = {"long_term_daily_digest"}

# ── Noise control ────────────────────────────────────────────────────────
# An alert you have learned to swipe past is worse than no alert: it trains
# you to ignore the channel that matters. These events fire on ordinary,
# expected conditions — a size that rounds below an exchange minimum, a
# rejected order, a slippage reading — and the bot re-emits them every
# 15-second loop iteration. Left alone they bury the two messages a day
# that actually need you. So they are aggregated and rate-limited: the first
# occurrence is sent, repeats of the SAME condition within the window are
# counted, and a summary lands at the next 6-hour report.
#
# Anything NOT in this set is sent immediately, every time. A halt, a lost
# connection and a blown loss limit are not noise and are never throttled.
THROTTLED_EVENTS = {
    "trade_rejected":     {"cooldown_sec": 900,  "label": "rejected orders"},
    "high_slippage":      {"cooldown_sec": 1800, "label": "slippage warnings"},
    "api_failure_burst":  {"cooldown_sec": 900,  "label": "API failure bursts"},
    "position_drift":     {"cooldown_sec": 1800, "label": "position drift"},
    "nse_alert":          {"cooldown_sec": 3600, "label": "NSE alerts"},
    "arbitrage_opportunity": {"cooldown_sec": 3600, "label": "arbitrage scans"},
    "portfolio_rotation": {"cooldown_sec": 3600, "label": "rotation signals"},
    "options_signal":     {"cooldown_sec": 3600, "label": "options signals"},
    "long_term_signal":   {"cooldown_sec": 21600, "label": "long-term signals"},
    "warning":            {"cooldown_sec": 1800, "label": "warnings"},
}

# Process-wide notifier, so modules that have no access to the instance
# (structured_loggers) can still raise alerts. Set by Notifier.__init__.
_global_notifier = None

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
    "bg_tile":      "#22263a",
    "text_primary": "#ffffff",
    "text_secondary":"#a0a0b0",
    "accent_green": "#00d68f",
    "accent_red":   "#ff4757",
    "accent_blue":  "#3b82f6",
    "accent_orange":"#ff9f43",
    "accent_purple":"#a855f7",
    "accent_cyan":  "#22d3ee",
    "border":       "#2d2f3e",
}

BRAND = "Wamucheha"


def _esc(value) -> str:
    """HTML-escape any interpolated value. Every piece of data reaching a
    template is untrusted — symbols, broker error strings, sentiment labels —
    so escaping happens once here rather than being remembered per template."""
    return html_lib.escape(str(value), quote=True)


# ── HTML email builder ───────────────────────────────────────────────────

def _preheader(text: str) -> str:
    """Preview text shown next to the subject in the inbox. Hidden in the body
    via zero-size + clipped divs; Gmail/Outlook fall back to it when no real
    content precedes it, so the digest's first visible line is never the
    greeting boilerplate."""
    filler = "&nbsp;" * 12
    return (f'<div style="display:none;font-size:1px;line-height:1px;max-height:0;'
            f'max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{_esc(text)}</div>'
            f'<div style="display:none;max-height:0;overflow:hidden;">{filler}</div>')


def _email_header(title: str, subtitle: str = "", color: str = None,
                  badge: str = "") -> str:
    color = color or EMAIL_COLORS["bg_header"]
    subtitle_html = (f'<p style="margin:6px 0 0;color:#c9c9d6;font-size:14px;line-height:1.5;">{subtitle}</p>'
                     if subtitle else "")
    badge_html = (
        f'<div style="display:inline-block;margin-bottom:10px;padding:4px 12px;'
        f'background:rgba(255,255,255,0.18);border-radius:20px;color:#ffffff;'
        f'font-size:11px;font-weight:700;letter-spacing:1.2px;">{_esc(badge.upper())}</div>'
        if badge else "")
    return f"""
    <div style="background:{color};padding:30px 32px 26px;border-radius:14px 14px 0 0;text-align:center;">
      <img src="https://img.icons8.com/fluency/48/chart-upward.png" width="42" height="42"
           style="margin-bottom:10px;filter:brightness(0) invert(1);" alt="{_esc(BRAND)} logo"/>
      {badge_html}
      <h1 style="margin:0;color:#ffffff;font-size:23px;font-weight:800;letter-spacing:0.3px;line-height:1.3;">{_esc(title)}</h1>
      {subtitle_html}
    </div>"""


def _email_footer(note: str = "") -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    note_html = (f'<p style="margin:8px 0 0;color:#4b5563;font-size:11px;line-height:1.6;">{note}</p>'
                 if note else "")
    return f"""
    <div style="background:{EMAIL_COLORS['bg_footer']};padding:20px 32px;border-radius:0 0 14px 14px;text-align:center;border-top:1px solid {EMAIL_COLORS['border']};">
      <img src="https://img.icons8.com/fluency/20/chart-upward.png" width="18" height="18"
           style="vertical-align:middle;margin-right:6px;filter:brightness(0) invert(0.7);" alt="logo"/>
      <span style="color:#6b7280;font-size:12px;font-weight:600;">{_esc(BRAND)} Trading Bot</span>
      <span style="color:#3d3f50;font-size:12px;margin:0 8px;">|</span>
      <span style="color:#6b7280;font-size:12px;">{now}</span>
      {note_html}
      <p style="margin:8px 0 0;color:#4b5563;font-size:11px;">
        Automated alerts &mdash; Do not reply directly to this email.
      </p>
    </div>"""


def _kv_row(label: str, value: str, mono: bool = False) -> str:
    font = "font-family:'Courier New',Courier,monospace;font-size:13px;" if mono else "font-size:14px;"
    return f"""
    <tr>
      <td style="padding:6px 0;color:{EMAIL_COLORS['text_secondary']};font-size:13px;width:130px;vertical-align:top;">{_esc(label)}</td>
      <td style="padding:6px 0;color:{EMAIL_COLORS['text_primary']};{font}">{value}</td>
    </tr>"""


def _section_divider(title: str) -> str:
    return f"""
    <tr>
      <td colspan="2" style="padding:18px 0 8px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="border-bottom:1px solid {EMAIL_COLORS['border']};"></td>
          <td style="padding:0 12px;color:{EMAIL_COLORS['accent_purple']};font-size:11px;font-weight:700;letter-spacing:1.4px;white-space:nowrap;">{_esc(title)}</td>
          <td style="border-bottom:1px solid {EMAIL_COLORS['border']};"></td>
        </tr></table>
      </td>
    </tr>"""


def _stat_tiles(tiles: list) -> str:
    """Row of headline numbers. Each tile is (value, label, color); built from
    nested tables + inline styles so it survives Outlook's Word renderer.
    Most email clients drop CSS grid/flex, so width is hard-set on the cells
    and they wrap rather than collapse when the row is too tight."""
    if not tiles:
        return ""
    cells = ""
    for value, label, color in tiles:
        cells += f"""
        <td width="25%" align="center" style="padding:0 5px;vertical-align:top;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:{EMAIL_COLORS['bg_tile']};border:1px solid {EMAIL_COLORS['border']};border-radius:10px;">
            <tr><td align="center" style="padding:12px 6px 10px;">
              <div style="color:{color};font-size:20px;font-weight:800;line-height:1.2;letter-spacing:-0.4px;">{value}</div>
            </td></tr>
            <tr><td align="center" style="padding:0 6px 12px;">
              <div style="color:{EMAIL_COLORS['text_secondary']};font-size:10px;font-weight:600;letter-spacing:0.8px;text-transform:uppercase;">{_esc(label)}</div>
            </td></tr>
          </table>
        </td>"""
    return f"""
    <tr><td style="padding:4px 0 10px;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>
    </td></tr>"""


def _score_bar(score, color: str = None, width: int = 56) -> str:
    """Inline visual for a 0-100 rating. Rendered as a fixed-width track with
    a filled cell sized in percent, using a spacer table — the only bar
    construction that works without CSS in Gmail and Outlook."""
    if score is None:
        return '<span style="color:#6b7280;">n/a</span>'
    score = max(0.0, min(100.0, float(score)))
    color = color or _score_color(score)
    pct = max(3, round(score))
    return f"""
    <table width="{width}" cellpadding="0" cellspacing="0" style="display:inline-table;vertical-align:middle;">
      <tr>
        <td width="{width}" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#2d2f3e;border-radius:4px;">
            <tr><td width="{pct}%" style="height:7px;line-height:7px;font-size:0;background:{color};border-radius:4px;">&nbsp;</td>
                <td style="height:7px;line-height:7px;font-size:0;">&nbsp;</td></tr>
          </table>
        </td>
        <td style="padding-left:7px;color:{color};font-size:12px;font-weight:700;vertical-align:middle;">{score:.0f}</td>
      </tr>
    </table>"""


def _score_color(score) -> str:
    if score is None:
        return EMAIL_COLORS["text_secondary"]
    if score >= 75:
        return EMAIL_COLORS["accent_green"]
    if score >= 55:
        return EMAIL_COLORS["accent_cyan"]
    if score >= 40:
        return EMAIL_COLORS["accent_orange"]
    return EMAIL_COLORS["accent_red"]


def _build_email_body(header_html: str, rows_html: str, footer_html: str,
                      preheader: str = "", width: int = 520) -> str:
    pre = _preheader(preheader) if preheader else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{_esc(BRAND)}</title></head>
<body style="margin:0;padding:0;background:{EMAIL_COLORS['bg_body']};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%;">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{EMAIL_COLORS['bg_body']};padding:24px 0;">
<tr><td align="center" style="padding:0 12px;">
<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" style="width:100%;max-width:{width}px;background:{EMAIL_COLORS['bg_card']};border-radius:14px;border:1px solid {EMAIL_COLORS['border']};">
  <tr><td>{header_html}</td></tr>
  <tr><td style="padding:24px 28px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows_html}</table>
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
    pnl_color = EMAIL_COLORS["accent_red"]
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

    # R:R is the single most useful number on an entry alert — how much the
    # target pays for each unit risked, before fees.
    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)
    if risk > 0:
        rr = reward / risk
        rows += _kv_row("Risk / Reward", f'<span style="color:#fff;font-weight:600;">1 : {rr:.2f}</span>')
    if entry_price:
        rows += _kv_row("Notional", f'${amount * entry_price:,.2f}')
    rows += _kv_row("Stop Distance", f'{abs(entry_price - stop_loss) / entry_price * 100:.2f}%')

    header = _email_header(
        f"{'🟢' if is_buy else '🔴'} Trade Opened — {symbol}",
        f"{side_label} {amount:.4f} on {exchange.upper()}",
        header_color,
    )
    footer = _email_footer()
    return _build_email_body(
        header, rows, footer,
        preheader=f"{side_label} {symbol} on {exchange.upper()} at {entry_price} — stop {stop_loss}, target {take_profit}",
    )


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
    rows += _kv_row("Pair", f'<span style="color:#fff;font-weight:600;">{_esc(symbol)}</span>')
    rows += _kv_row("Side", _esc(side.upper()))
    rows += _kv_row("Entry Price", _esc(entry_price), mono=True)
    rows += _kv_row("Exit Price", _esc(exit_price), mono=True)
    rows += _kv_row("PnL", f'<span style="color:{pnl_color};font-weight:700;font-size:15px;">{pnl:+.2f} USD ({pnl_pct:+.2f}%)</span>', mono=True)
    rows += _kv_row("Notional", f'${amount * entry_price:,.2f}')
    rows += _kv_row("Held Return", f'<span style="color:{pnl_color};font-weight:600;">{pnl_pct:+.2f}%</span>')
    rows += _kv_row("Reason", _esc(reason or "N/A"))
    rows += _kv_row("Exchange", _esc(exchange.upper()))

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
    return _build_email_body(
        header, rows, footer,
        preheader=f"{symbol} closed {result_label.lower()} — {pnl:+.2f} USD ({pnl_pct:+.2f}%)",
    )


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
    rows += _stat_tiles([
        (str(trades), "trades", EMAIL_COLORS["accent_blue"]),
        (f"{wins}/{losses}", "w / l", EMAIL_COLORS["accent_green"]),
        (f"{pnl:+.2f}", "hour pnl", pnl_color),
        (f"${balance:,.0f}", "balance", EMAIL_COLORS["accent_purple"]),
    ])
    rows += _kv_row("Status", f'<span style="color:{status_color};font-weight:700;">● {status_label}</span>')
    rows += _kv_row("Time", _esc(str(summary.get("ts", "N/A"))[:19] + "Z"))
    rows += _section_divider("HOURLY PERFORMANCE")
    rows += _kv_row("Trades", str(trades))
    rows += _kv_row("Wins / Losses", f"{wins} / {losses}")
    hour_wr = (wins / trades * 100) if trades > 0 else 0
    rows += _kv_row("Hour Win Rate", f"{hour_wr:.1f}%")
    rows += _kv_row("Hour PnL", f'<span style="color:{pnl_color};font-weight:600;">{pnl:+.2f} USD</span>')
    rows += _section_divider("ACCOUNT STATUS")
    rows += _kv_row("Balance", f'<span style="color:#fff;font-weight:600;">${balance:,.2f}</span>')
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
    return _build_email_body(
        header, rows, footer,
        preheader=f"{trades} trades, {wins}W/{losses}L, PnL {pnl:+.2f} USD, balance ${balance:,.2f}",
    )


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
    rows += _kv_row("Event", f'<span style="color:#fff;font-weight:600;">{_esc(event_type)}</span>')
    rows += _kv_row("Raised", _esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
    rows += _section_divider("ALERT DETAILS")
    rows += f"""<tr><td colspan="2" style="padding:12px 0;color:{EMAIL_COLORS['text_primary']};font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word;">{_esc(message)}</td></tr>"""

    header = _email_header(
        f"{'🚨' if priority in ('critical','high') else 'ℹ️'} {event_type.replace('_', ' ').title()}",
        f"Priority: {priority_label}",
        color,
    )
    footer = _email_footer()
    return _build_email_body(header, rows, footer, preheader=message[:140])


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
    if event_type == "long_term_daily_digest":
        return f"📅 Daily Investing Digest — {datetime.now(timezone.utc):%a %d %b %Y}"
    return f"{prefix} [{event_type.replace('_', ' ').title()}] {BRAND} Bot"


def _held_seconds(opened_at: str) -> float:
    if not opened_at:
        return 0.0
    try:
        opened = datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds())
    except (ValueError, TypeError):
        return 0.0


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

        global _global_notifier
        _global_notifier = self

        # Attached after construction (main.py builds the notifier before the
        # state manager). Without it the daily numbers fall back to the
        # in-process session counters only.
        self.state = None
        self.fee_model = None
        self._suppressed: dict = {}

        self._session_stats = {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "start_balance": 0.0,
            "total_profit": 0.0, "total_loss": 0.0,
            "gross_pnl": 0.0, "fees_paid": 0.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

    def attach_state(self, state_manager, fee_model=None):
        """Give the notifier a live handle on the DB and the cost model, so
        'today' in a message means today in the ledger rather than today in
        this process's memory."""
        self.state = state_manager
        self.fee_model = fee_model

    def update_start_balance(self, balance: float):
        self._session_stats["start_balance"] = balance

    # ── Noise suppression ────────────────────────────────────────────────

    @staticmethod
    def suppressed_counts() -> dict:
        return dict(_global_notifier._suppressed) if _global_notifier else {}

    def notify(self, event_type: str, message: str, priority: str = "normal",
               trade_data: dict = None):
        if self._should_suppress(event_type, message):
            return
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

    def _should_suppress(self, event_type: str, message: str) -> bool:
        """True when this exact condition was already reported inside the
        event's cooldown. The repeat is counted, not sent — the count is
        surfaced in the next 6-hour report so silence never hides a fault."""
        rule = THROTTLED_EVENTS.get(event_type)
        if not rule:
            return False
        now = datetime.now(timezone.utc).timestamp()
        # Keyed on the reason, not the whole message: a run loop re-emitting
        # "below exchange minimum" is one problem said 400 times, and must not
        # be able to bypass the throttle by rewording itself.
        key = f"{event_type}:{message[:80]}"
        entry = self._suppressed.get(key)
        if entry and now - entry["last"] < rule["cooldown_sec"]:
            entry["count"] += 1
            return True
        if entry:
            entry["count"] += 1
        else:
            self._suppressed[key] = {
                "event": event_type, "label": rule["label"],
                "count": 1, "last": now, "last_seen": now,
            }
        return False

    def drain_suppressed(self) -> list:
        """Pop the aggregated repeats, so the next report can state what was
        held back and how much."""
        out = list(self._suppressed.values())
        self._suppressed.clear()
        return [e for e in out if e["count"] > 1]

    # ── Long reports (daily digest, stock recommendations) ────────────────

    def notify_report(self, event_type: str, telegram_html: str, subject: str = None,
                      email_html: str = None):
        """Multi-section report to every channel. notify() sends one message,
        which Telegram (4096 chars) and Discord (2000 chars) reject when a
        digest is longer — here it's split on line boundaries, and Discord
        gets markdown instead of raw HTML tags."""
        self._log({"type": event_type, "message": telegram_html, "priority": "normal",
                   "ts": datetime.now(timezone.utc).isoformat()})

        def chunks(text: str, limit: int):
            part = ""
            for line in text.split("\n"):
                if part and len(part) + len(line) + 1 > limit:
                    yield part
                    part = ""
                part += line[:limit] + "\n"
            if part.strip():
                yield part

        if self.telegram_token and self.telegram_chat_id:
            for part in chunks(telegram_html, 3900):
                try:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                        json={"chat_id": self.telegram_chat_id, "text": part,
                              "parse_mode": "HTML", "disable_web_page_preview": True},
                        timeout=15)
                    if not resp.ok:
                        logger.error(f"Telegram report chunk failed: {resp.text[:200]}")
                except Exception as e:
                    logger.error(f"Telegram report failed: {e}")

        if self.discord_webhook_url:
            md = re.sub(r"</?b>", "**", telegram_html)
            md = re.sub(r"</?i>", "*", md)
            md = re.sub(r"</?code>", "`", md)
            md = html_lib.unescape(re.sub(r"<[^>]+>", "", md))
            for part in chunks(md, 1900):
                try:
                    requests.post(self.discord_webhook_url, json={"content": part}, timeout=15)
                except Exception as e:
                    logger.error(f"Discord report failed: {e}")

        cfg = self.email_cfg
        if event_type not in EMAIL_ALLOWED_EVENTS:
            return
        if cfg.get("address") and cfg.get("to"):
            try:
                body = email_html or _build_alert_email(event_type, telegram_html, "normal")
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject or _email_subject(event_type)
                msg["From"] = f"Wamucheha Bot <{cfg['address']}>"
                msg["To"] = cfg["to"]
                msg.attach(MIMEText(body, "html"))
                with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(cfg["address"], cfg["app_password"])
                    server.send_message(msg)
            except Exception as e:
                logger.error(f"Email report failed: {e}")

    # ── Daily context blocks ─────────────────────────────────────────────

    def daily_economics(self) -> dict:
        """Today's real economics, from the ledger when it is attached and
        from the in-process counters when it is not."""
        if self.state is not None:
            try:
                return self.state.get_daily_economics()
            except Exception as e:
                logger.warning(f"daily economics unavailable: {e}")
        s = self._session_stats
        return {
            "day": datetime.now(timezone.utc).date().isoformat(),
            "realized_pnl": s["total_pnl"], "gross_pnl": s.get("gross_pnl", s["total_pnl"]),
            "fees_paid": s.get("fees_paid", 0.0), "network_fees": 0.0,
            "trades": s["total_trades"], "wins": s["wins"], "losses": s["losses"],
        }

    def _daily_lines_telegram(self) -> str:
        daily = self.daily_economics()
        if self.fee_model is not None:
            try:
                daily["network_fees"] = self.fee_model.network_spend()["network_fee_usdt"]
            except Exception:
                pass
        balance = (self._session_stats["start_balance"] + self._session_stats["total_pnl"])
        return f.daily_block(daily, balance=balance)

    def _daily_footer(self) -> str:
        daily = self.daily_economics()
        net = daily.get("realized_pnl", 0.0) - daily.get("network_fees", 0.0)
        return (f"{f.E_CALENDAR} Today {f.arrow(net)} {f.signed_money(net)} USD  ·  "
                f"{daily.get('trades', 0)} trades  ·  {daily.get('wins', 0)}W "
                f"{daily.get('losses', 0)}L  ·  fees ${f.money(daily.get('fees_paid', 0.0), 4)}")

    # ── Trade open ────────────────────────────────────────────────────────

    def notify_trade_opened(self, symbol: str, side: str, amount: float,
                             entry_price: float, stop_loss: float, take_profit: float,
                             exchange: str, dry_run: bool = False, strategies: list = None,
                             score: float = 0, regime: str = "",
                             entry_fee: float = 0.0, round_trip_fee: float = 0.0):
        self._session_stats["total_trades"] += 1
        self._session_stats["fees_paid"] = self._session_stats.get("fees_paid", 0.0) + float(entry_fee or 0)

        notional = float(amount) * float(entry_price)
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr = (reward / risk) if risk > 0 else 0.0
        # What the target actually pays once the round trip is paid for. On a
        # 3% target at 10bps a side this is 2.80%, not 3.00% — small enough to
        # ignore per trade and decisive enough to change the system's expectancy
        # over a few hundred of them.
        net_target_pct = (reward - (round_trip_fee / notional * 100)) if notional else 0.0

        trade_data = {
            "symbol": symbol, "side": side, "amount": amount,
            "entry_price": entry_price, "stop_loss": stop_loss,
            "take_profit": take_profit, "exchange": exchange,
            "dry_run": dry_run, "entry_fee": entry_fee,
            "round_trip_fee": round_trip_fee, "notional": notional,
            "risk_reward": rr, "net_target_pct": net_target_pct,
            "strategies": strategies, "score": score, "regime": regime,
        }

        # ── Telegram ──────────────────────────────────────────────────────
        is_buy = side == "buy"
        mode = "📝 PAPER" if dry_run else "💰 LIVE"
        notional_pct_of_bal = (notional / self._session_stats["start_balance"] * 100) \
            if self._session_stats["start_balance"] else 0.0

        tg = [
            f"<b>{f.E_BUY if is_buy else f.E_SELL} {'LONG OPENED' if is_buy else 'SHORT OPENED'} "
            f"· {f.E_BOOK} {symbol}</b>   <i>{mode}</i>",
            f.rules(),
            f"{f.E_ENTRY} <b>Entry</b>   <code>{f.price(entry_price)}</code>",
            f"{f.E_SIZE} <b>Size</b>    <code>{f.qty(amount)} {symbol.split('/')[0]}</code>"
            f"  <i>({f.money(notional)} USD · {notional_pct_of_bal:.1f}% of balance)</i>",
            f"{f.E_VENUE} <b>Venue</b>   {exchange.upper()}",
            f"{f.E_TARGET} <b>Target</b>  <code>{f.price(take_profit)}</code>  "
            f"{f.ARROW_UP} <b>+{abs(take_profit - entry_price) / entry_price * 100:.2f}%</b>",
            f"{f.E_STOP} <b>Stop</b>    <code>{f.price(stop_loss)}</code>  "
            f"{f.ARROW_DOWN} <b>−{abs(stop_loss - entry_price) / entry_price * 100:.2f}%</b>",
        ]
        if rr:
            tg.append(f"{f.E_RR} <b>R:R</b>     <code>1 : {rr:.2f}</code>"
                      f"  <i>→ {net_target_pct:+.2f}% net of costs</i>")
        if round_trip_fee:
            tg.append(f"{f.E_FEES} <b>Fees</b>   <code>−{f.money(round_trip_fee, 4)} USD</code>"
                      f"  <i>round trip · paid in ${f.money(entry_fee, 4)} now, "
                      f"${f.money(round_trip_fee - entry_fee, 4)} on exit</i>")
        if strategies:
            tg.append(f"{f.E_BRAIN} <b>Why</b>    {f.E_TROPHY} {', '.join(strategies)}")
        if score:
            tg.append(f"{f.E_STATS} <b>Score</b>   <code>{score:.3f}</code>")
        if regime:
            tg.append(f"{f.E_GLOBE} <b>Regime</b>   {regime}")
        tg.append(f.rules())
        tg.append(self._daily_lines_telegram())
        self._send_telegram_styled("\n".join(tg))

        # ── Discord ───────────────────────────────────────────────────────
        color = COLOR_GREEN if is_buy else COLOR_RED
        fields = [
            {"name": f"{f.E_BOOK} Pair", "value": f"`{symbol}`", "inline": True},
            {"name": f"{f.E_STATS} Side", "value": "🟢 LONG" if is_buy else "🔴 SHORT", "inline": True},
            {"name": "📝 Mode", "value": mode, "inline": True},
            {"name": f"{f.E_ENTRY} Entry", "value": f"`{f.price(entry_price)}`", "inline": True},
            {"name": f"{f.E_SIZE} Size", "value": f"`{f.qty(amount)}`", "inline": True},
            {"name": f"{f.E_VENUE} Notional", "value": f"`${f.money(notional)}`", "inline": True},
            {"name": f"{f.E_TARGET} Target", "value": f"`{f.price(take_profit)}`  "
                                                  f"{f.ARROW_UP} `+{abs(take_profit - entry_price) / entry_price * 100:.2f}%`", "inline": True},
            {"name": f"{f.E_STOP} Stop", "value": f"`{f.price(stop_loss)}`  "
                                                 f"{f.ARROW_DOWN} `−{abs(stop_loss - entry_price) / entry_price * 100:.2f}%`", "inline": True},
            {"name": f"{f.E_RR} R:R", "value": f"`1 : {rr:.2f}`  ({net_target_pct:+.2f}% net)", "inline": True},
        ]
        if round_trip_fee:
            fields.append({"name": f"{f.E_FEES} Round-Trip Fee",
                           "value": f"`−${f.money(round_trip_fee, 4)}` "
                                    f"(in `${f.money(entry_fee, 4)}` / out `${f.money(round_trip_fee - entry_fee, 4)}`)",
                           "inline": True})
        if strategies:
            fields.append({"name": f"{f.E_BRAIN} Strategies", "value": ", ".join(strategies), "inline": False})
        if score:
            fields.append({"name": f"{f.E_STATS} Score", "value": f"`{score:.3f}`", "inline": True})
        if regime:
            fields.append({"name": f"{f.E_GLOBE} Regime", "value": regime, "inline": True})
        self._send_discord_embed(
            title=f"{f.E_BUY if is_buy else f.E_SELL} {'LONG' if is_buy else 'SHORT'} OPENED · {symbol}",
            description=f"**{f.qty(amount)} {symbol.split('/')[0]}** on **{exchange.upper()}** "
                        f"@ `{f.price(entry_price)}`  —  risk `${f.money(risk * amount, 4)}`, "
                        f"reward `${f.money(reward * amount, 4)}`",
            color=color,
            fields=fields,
            footer=self._daily_footer(),
            webhook_url=self.discord_webhook_trades,
        )

        # ── Event log ─────────────────────────────────────────────────────
        self._log_trade("opened", trade_data)

    # ── Trade close ───────────────────────────────────────────────────────

    def notify_trade_closed(self, symbol: str, side: str, amount: float,
                            entry_price: float, exit_price: float, pnl: float,
                            exchange: str, reason: str = "",
                            strategies: list = None,
                            gross_pnl: float = None, entry_fee: float = 0.0,
                            exit_fee: float = 0.0, fees_are_estimated: bool = False,
                            opened_at: str = None):
        """`pnl` is NET of venue fees. `gross_pnl` is what the price chart says.
        Both are shown: the gap between them is the cost of doing the trade,
        and a day that only ever shows the net number hides exactly the thing
        you need to see when the strategy starts bleeding."""
        is_win = pnl > 0
        gross = float(gross_pnl if gross_pnl is not None else pnl)
        total_fees = float(entry_fee or 0) + float(exit_fee or 0)
        notional = float(amount) * float(entry_price)

        if is_win:
            self._session_stats["wins"] += 1
            self._session_stats["total_profit"] += pnl
        else:
            self._session_stats["losses"] += 1
            self._session_stats["total_loss"] += pnl
        self._session_stats["total_pnl"] += pnl
        self._session_stats["gross_pnl"] = self._session_stats.get("gross_pnl", 0.0) + gross
        self._session_stats["fees_paid"] = self._session_stats.get("fees_paid", 0.0) + total_fees
        self._session_stats["best_trade"] = max(self._session_stats["best_trade"], pnl)
        self._session_stats["worst_trade"] = min(self._session_stats["worst_trade"], pnl)

        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if side == "buy" \
            else ((entry_price - exit_price) / entry_price * 100)
        fee_pct = (total_fees / notional * 100) if notional else 0.0
        held = _held_seconds(opened_at)
        icon = f.E_PROFIT if is_win else f.E_LOSS
        result_text = "PROFIT" if is_win else "LOSS"

        trade_data = {
            "symbol": symbol, "side": side, "amount": amount,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl": pnl, "pnl_pct": pnl_pct, "exchange": exchange,
            "reason": reason, "gross_pnl": gross,
            "entry_fee": entry_fee, "exit_fee": exit_fee, "fees": total_fees,
            "fees_are_estimated": fees_are_estimated,
            "held_seconds": held, "notional": notional,
            "strategies": strategies,
        }

        # ── Telegram ──────────────────────────────────────────────────────
        s = self._session_stats
        wr = s['wins'] / max(1, s['wins'] + s['losses']) * 100
        total_money = s['start_balance'] + s['total_pnl']

        tg = [
            f"<b>{icon} CLOSED {'▲ IN PROFIT' if is_win else '▼ IN LOSS'} · {symbol}</b>",
            f.rules(),
            f"{'📈 LONG' if side == 'buy' else '📉 SHORT'}  <code>{f.qty(amount)}</code>  "
            f"{f.ARROW_RIGHT}  {f.E_VENUE} {exchange.upper()}",
            f"{f.E_ENTRY} <code>{f.price(entry_price)}</code>  "
            f"{f.ARROW_RIGHT}  {f.E_EXIT} <code>{f.price(exit_price)}</code>  "
            f"{f.arrow(pnl_pct)} <b>{f.signed_pct(pnl_pct)}</b>",
            f.rules(),
            f"💰 <b>Gross</b>    <code>{f.arrow(gross)} {f.signed_money(gross)} USD</code>"
            f"  <i>(price move only)</i>",
            f"{f.E_FEES} <b>Fees</b>     <code>{f.arrow(-total_fees)} {f.signed_money(-total_fees, 4)} USD</code>"
            f"  <i>−{fee_pct:.2f}% · in ${f.money(entry_fee, 4)} / out ${f.money(exit_fee, 4)}"
            f"{' · estimated' if fees_are_estimated else ''}</i>",
            f"🧾 <b>NET</b>      <code>{f.arrow(pnl)} {f.signed_money(pnl)} USD "
            f"({f.signed_pct(pnl / notional * 100 if notional else 0)})</b>",
            f"{f.E_TIME} <b>Held</b>     {f.holding_time(held)}   ·   🏁 <b>Exit</b> {reason or 'manual'}",
            f.rules(),
            f"📅 <b>Today</b>  {f.arrow(s['total_pnl'])} <code>{f.signed_money(s['total_pnl'])} USD</code>"
            f"  <i>· {s['wins']}W {s['losses']}L · {wr:.0f}% WR</i>",
            f"⛽ <b>Costs today</b>  <code>{f.signed_money(-s.get('fees_paid', 0.0), 4)} USD</code>",
            f"💼 <b>Balance</b>  <code>{f.money(total_money)} USD</code>",
        ]
        if strategies:
            tg.append(f"{f.E_BRAIN} <b>Strategy</b>  {', '.join(strategies)}")
        self._send_telegram_styled("\n".join(tg))

        # ── Discord ───────────────────────────────────────────────────────
        color = COLOR_GREEN if is_win else COLOR_RED
        fields = [
            {"name": f"{f.E_BOOK} Pair", "value": f"`{symbol}`", "inline": True},
            {"name": f"{f.E_STATS} Side", "value": "📈 LONG" if side == "buy" else "📉 SHORT", "inline": True},
            {"name": "🎯 Result", "value": f"{icon} **{result_text}**", "inline": True},
            {"name": f"{f.E_ENTRY} Entry", "value": f"`{f.price(entry_price)}`", "inline": True},
            {"name": f"{f.E_EXIT} Exit", "value": f"`{f.price(exit_price)}`", "inline": True},
            {"name": "📊 Move", "value": f"{f.arrow(pnl_pct)} `{f.signed_pct(pnl_pct)}`", "inline": True},
            {"name": f"{f.E_SIZE} Size", "value": f"`{f.qty(amount)}` (${f.money(notional)})", "inline": True},
            {"name": f"{f.E_VENUE} Venue", "value": exchange.upper(), "inline": True},
            {"name": f"{f.E_TIME} Held", "value": f.holding_time(held), "inline": True},
            {"name": "💰 Gross", "value": f"{f.arrow(gross)} `{f.signed_money(gross)} USD`", "inline": True},
            {"name": f"{f.E_FEES} Fees", "value": f"`{f.signed_money(-total_fees, 4)} USD` "
                                                f"(−{fee_pct:.2f}%)"
                                                f"{' · estimated' if fees_are_estimated else ''}", "inline": True},
            {"name": "🧾 NET", "value": f"{f.arrow(pnl)} **`{f.signed_money(pnl)} USD`**", "inline": True},
            {"name": "🏁 Exit reason", "value": reason or "manual", "inline": True},
        ]
        if strategies:
            fields.append({"name": f"{f.E_BRAIN} Strategies", "value": ", ".join(strategies), "inline": False})
        self._send_discord_embed(
            title=f"{icon} CLOSED {result_text} · {symbol}",
            description=f"**{side.upper()}** {f.qty(amount)} on {exchange.upper()} — "
                        f"`{f.price(entry_price)}` {f.ARROW_RIGHT} `{f.price(exit_price)}` "
                        f"({f.signed_pct(pnl_pct)}) → net `{f.signed_money(pnl)} USD`",
            color=color,
            fields=fields,
            footer=self._daily_footer(),
            webhook_url=self.discord_webhook_trades,
        )

        # ── Event log ─────────────────────────────────────────────────────
        self._log_trade("closed", trade_data)

    # ── 6-hour report ─────────────────────────────────────────────────────

    def notify_window_report(self, report: dict):
        """The mid-session report. It exists to answer one question — 'is this
        working?' — using the numbers that decide it: net result AFTER costs,
        the win rate, and what execution has cost so far. Deliberately not a
        trade-by-trade recap; every trade was already messaged the moment it
        closed, and repeating them here is how a report becomes noise."""
        window = report.get("window_hours", 6)
        pnl = float(report.get("pnl", 0.0))
        gross = float(report.get("gross_pnl", pnl))
        fees = float(report.get("fees", 0.0))
        trades = int(report.get("trades", 0))
        wins = int(report.get("wins", 0))
        losses = int(report.get("losses", 0))
        wr = (wins / trades * 100) if trades else 0.0
        balance = float(report.get("trading_balance", 0.0))
        open_pos = int(report.get("open_positions", 0))
        halted = bool(report.get("trading_halted", False))
        daily = report.get("daily", {})
        suppressed = report.get("suppressed", [])

        # Below this many trades the win rate is noise, not evidence. Saying
        # "50% win rate" off two trades is how a report loses the reader's
        # trust for the numbers that would have mattered.
        verdict, verdict_icon, verdict_color = self._verdict(pnl, trades, fees)

        tg = [
            f"<b>{f.E_CLOCK} {window}-HOUR REPORT</b>   <i>{f.utc_stamp(report.get('epoch'))}</i>",
            f.rules(),
            f"🧾 <b>NET</b>   {f.arrow(pnl)} <code>{f.signed_money(pnl)} USD</code>"
            f"   <i>{f.signed_pct(pnl / balance * 100 if balance else 0)} of balance</i>",
            f"💰 <b>GROSS</b>  <code>{f.signed_money(gross)} USD</code>",
            f"⛽ <b>FEES</b>   <code>{f.signed_money(-fees, 4)} USD</code>"
            f"   <i>{fees / gross * 100 if gross > 0 else 0:.1f}% of gross profit eaten</i>",
            f"📊 <b>RECORD</b>  <code>{trades}</code> trades  ·  🟢 {wins}W / 🔴 {losses}L"
            f"  ·  <b>{wr:.0f}% WR</b>" + ("  <i>(too few trades to judge)</i>" if trades < 5 else ""),
            f.rules(),
            f"💼 <b>Balance</b>  <code>{f.money(balance)} USD</code>"
            f"   ·   📂 open positions: <code>{open_pos}</code>",
            f"📅 <b>Today</b>    {f.arrow(daily.get('realized_pnl', 0))} "
            f"<code>{f.signed_money(daily.get('realized_pnl', 0))} USD</code>",
            f"{'🔴 <b>Status</b>   HALTED — ' + str(report.get('halt_reason') or 'see dashboard')}"
            if halted else f"{f.E_POWER} <b>Status</b>   RUNNING",
        ]
        tg.append(f.rules())
        tg.append(f"<b>{verdict_icon} {verdict}</b>")
        if open_pos:
            exposure = float(report.get("open_notional", 0.0))
            tg.append(f"📂 <i>{open_pos} position(s) hold ${f.money(exposure)} notional — "
                      f"stops are live on every one</i>")
        if suppressed:
            held = ", ".join(f"{e['label']} ×{e['count']}" for e in suppressed[:4])
            tg.append(f"🔕 <i>Held back to keep this channel clean: {held}</i>")
        self._send_telegram_styled("\n".join(tg))

        color = COLOR_RED if pnl < 0 else (COLOR_GREEN if pnl > 0 else COLOR_GRAY)
        fields = [
            {"name": "🧾 Net (after costs)", "value": f"{f.arrow(pnl)} `{f.signed_money(pnl)} USD`", "inline": True},
            {"name": "💰 Gross", "value": f"`{f.signed_money(gross)} USD`", "inline": True},
            {"name": "⛽ Fees paid", "value": f"`{f.signed_money(-fees, 4)} USD`", "inline": True},
            {"name": "📊 Trades", "value": f"`{trades}`  ({wins}W / {losses}L, {wr:.0f}%)", "inline": True},
            {"name": "💼 Balance", "value": f"`{f.money(balance)} USD`", "inline": True},
            {"name": "📂 Open", "value": f"`{open_pos}` position(s)", "inline": True},
            {"name": "📅 Today", "value": f"{f.arrow(daily.get('realized_pnl', 0))} "
                                          f"`{f.signed_money(daily.get('realized_pnl', 0))} USD`", "inline": True},
            {"name": "🔌 Status", "value": "🔴 HALTED" if halted else "🟢 RUNNING", "inline": True},
            {"name": f"{verdict_icon} Verdict", "value": verdict, "inline": False},
        ]
        if suppressed:
            fields.append({"name": "🔕 Suppressed repeats",
                           "value": ", ".join(f"{e['label']} ×{e['count']}" for e in suppressed[:6]),
                           "inline": False})
        self._send_discord_embed(
            title=f"{f.E_CLOCK} {window}-Hour Report · {f.arrow(pnl)} {f.signed_money(pnl)} USD",
            description=verdict,
            color=color,
            fields=fields,
            footer=f"{f.E_CALENDAR} Today {f.signed_money(daily.get('realized_pnl', 0))} USD net  ·  "
                   f"fees {f.signed_money(-fees, 4)} USD",
            webhook_url=self.discord_webhook_trades,
        )

        self._log({
            "type": f"window_{window}h_report",
            "message": f"{window}h: {trades} trades, net {pnl:+.2f}, fees {fees:.4f}, balance {balance:.2f}",
            "priority": "normal",
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_data": report,
        })

    # ── 24-hour report ────────────────────────────────────────────────────

    def notify_daily_report(self, report: dict):
        """End-of-day state of the world. The first line is uptime, because
        'was the bot even on?' is the question that has to be answered before
        any P&L number on the same screen can be believed."""
        uptime = float(report.get("uptime_seconds", 0.0))
        health = report.get("health", "unknown")     # running | halted | starting
        pnl = float(report.get("pnl", 0.0))
        fees = float(report.get("fees", 0.0))
        gas = float(report.get("network_fees", 0.0))
        balance = float(report.get("trading_balance", 0.0))
        start_balance = float(report.get("start_balance", balance))
        trades = int(report.get("trades", 0))
        wins = int(report.get("wins", 0))
        losses = int(report.get("losses", 0))
        week = report.get("week", [])
        all_time = report.get("all_time", {})
        restarts = int(report.get("restarts", 0))
        open_pos = int(report.get("open_positions", 0))
        net = pnl - gas
        return_pct = (net / start_balance * 100) if start_balance else 0.0

        health_line, health_icon = {
            "running": (f"{f.E_POWER} <b>RUNNING</b>  ·  up {f.holding_time(uptime)}", "🟢"),
            "halted": (f"🔴 <b>HALTED</b>  ·  up {f.holding_time(uptime)}  — "
                       f"{report.get('halt_reason') or 'no reason recorded'}", "🔴"),
            "starting": (f"🟡 <b>STARTED</b>  ·  first heartbeat", "🟡"),
        }.get(health, (f"🟡 {health}", "🟡"))

        week_rows = "".join(
            f"\n  {f.E_CALENDAR} <code>{d['day'][5:]}</code>  "
            f"{f.arrow(d['realized_pnl'])} <code>{f.signed_money(d['realized_pnl'])}</code>"
            f"  <i>· {d['trades']}t · {d['wins']}W {d['losses']}L · fees ${f.money(d['fees_paid'], 4)}</i>"
            for d in week
        ) or "\n  <i>no closed trades this week</i>"

        verdict, verdict_icon, _ = self._verdict(pnl, trades, fees, gas)

        tg = [
            f"<b>📅 24-HOUR REPORT</b>   <i>{f.utc_stamp(report.get('epoch'))}</i>",
            f.rules(),
            health_line,
            f"🔄 <b>Restarts</b>  <code>{restarts}</code>"
            f"  <i>{'— stable' if restarts == 0 else '— investigate if unexpected'}</i>",
            f.rules(),
            f"🧾 <b>TODAY'S NET</b>  {f.arrow(net)} <code>{f.signed_money(net)} USD</code>"
            f"   <i>({f.signed_pct(return_pct)} on {f.money(start_balance)} start)</i>",
            f"💰 <b>Gross</b>  <code>{f.signed_money(report.get('gross_pnl', pnl))} USD</code>",
            f"⛽ <b>Venue fees</b>  <code>{f.signed_money(-fees, 4)} USD</code>",
            f"⛓ <b>Network (gas)</b>  <code>{f.signed_money(-gas, 4)} USD</code>"
            f"  <i>· {report.get('network', '')}</i>",
            f"💸 <b>TOTAL COST</b>  <code>{f.signed_money(-(fees + gas), 4)} USD</code>",
            f"📊 <b>Record</b>  {trades} trades  ·  🟢 {wins}W / 🔴 {losses}L  ·  "
            f"<b>{(wins / trades * 100) if trades else 0:.0f}% WR</b>",
            f"💼 <b>Balance</b>  <code>{f.money(balance)} USD</code>"
            f"   ·   📂 <code>{open_pos}</code> open",
            f.rules(),
            f"📈 <b>LAST 7 DAYS</b>",
            week_rows,
            f.rules(),
            f"💎 <b>All time</b>  {all_time.get('total', 0)} trades  ·  "
            f"net {f.arrow(all_time.get('net_pnl', 0))} <code>{f.signed_money(all_time.get('net_pnl', 0))} USD</code>"
            f"  ·  fees paid <code>${f.money(all_time.get('total_fees', 0), 2)}</code>",
            f.rules(),
            f"<b>{verdict_icon} {verdict}</b>",
        ]
        self._send_telegram_styled("\n".join(tg))

        color = COLOR_RED if pnl < 0 else (COLOR_GREEN if pnl > 0 else COLOR_GRAY)
        fields = [
            {"name": "🔌 Uptime", "value": f"{health_icon} **{health.upper()}** · {f.holding_time(uptime)}", "inline": True},
            {"name": "🔄 Restarts", "value": f"`{restarts}`", "inline": True},
            {"name": "🧾 Net today", "value": f"{f.arrow(net)} `{f.signed_money(net)} USD`", "inline": True},
            {"name": "💰 Gross", "value": f"`{f.signed_money(report.get('gross_pnl', pnl))} USD`", "inline": True},
            {"name": "⛽ Venue fees", "value": f"`{f.signed_money(-fees, 4)} USD`", "inline": True},
            {"name": "⛓ Gas", "value": f"`{f.signed_money(-gas, 4)} USD`", "inline": True},
            {"name": "💸 Total cost", "value": f"`{f.signed_money(-(fees + gas), 4)} USD`", "inline": True},
            {"name": "📊 Record", "value": f"`{trades}` · {wins}W / {losses}L", "inline": True},
            {"name": "💼 Balance", "value": f"`{f.money(balance)} USD`", "inline": True},
            {"name": "📂 Open", "value": f"`{open_pos}`", "inline": True},
            {"name": "💎 All-time net", "value": f"{f.arrow(all_time.get('net_pnl', 0))} "
                                                f"`{f.signed_money(all_time.get('net_pnl', 0))} USD`", "inline": True},
            {"name": f"{verdict_icon} Verdict", "value": verdict, "inline": False},
        ]
        if week:
            fields.append({
                "name": "📈 Last 7 days",
                "value": "\n".join(
                    f"{f.arrow(d['realized_pnl'])} `{d['day']}` "
                    f"{f.signed_money(d['realized_pnl'])} USD · {d['trades']}t "
                    f"({d['wins']}W {d['losses']}L) · fees ${f.money(d['fees_paid'], 4)}"
                    for d in week),
                "inline": False,
            })
        self._send_discord_embed(
            title=f"📅 24-Hour Report · {health_icon} {health.upper()} · {f.arrow(net)} {f.signed_money(net)} USD",
            description=verdict,
            color=color,
            fields=fields,
            footer=f"Uptime {f.holding_time(uptime)} · restarts {restarts} · "
                   f"fees+gas ${f.money(fees + gas, 2)}",
            webhook_url=self.discord_webhook_trades,
        )

        self._log({
            "type": "daily_24h_report",
            "message": f"24h: {health}, uptime {uptime / 3600:.1f}h, {trades} trades, net {net:+.2f}, "
                       f"fees {fees:.4f}, gas {gas:.4f}, restarts {restarts}",
            "priority": "normal",
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_data": report,
        })

    @staticmethod
    def _verdict(pnl: float, trades: int, fees: float, gas: float = 0.0):
        """One honest sentence. Every number in the report is already on
        screen; what the reader wants is the conclusion, stated plainly, with
        its basis attached so it can be argued with."""
        if trades == 0:
            return ("No trades closed in this window — nothing was gained or lost, "
                    "but the strategy found no qualifying entry. Confirm this is a "
                    "signals problem, not a data or venue problem.", "🟡", COLOR_YELLOW)
        net = pnl - gas
        total_costs = fees + gas
        fee_drag = (total_costs / pnl * 100) if pnl > 0 and total_costs > 0 else 0.0
        if net > 0:
            msg = f"Net positive: {f.signed_money(net)} USD after {f.money(total_costs, 4)} in costs"
            if fee_drag > 60:
                msg += (f". Costs consumed {fee_drag:.0f}% of the gross gain — the edge is real "
                        f"but thin, and it will not survive a fee increase or a worse fill.")
                return msg, "🟡", COLOR_YELLOW
            return msg + ". Worth continuing at this size.", "🟢", COLOR_GREEN
        if pnl > 0 and net <= 0:
            return (f"The price moves said {f.signed_money(pnl)} USD, but {f.money(total_costs, 4)} "
                    f"in fees and gas took it to {f.signed_money(net)} USD. This is a cost problem, "
                    "not a signal problem — cut size, cut trade count, or get a lower fee tier.",
                    "🟡", COLOR_YELLOW)
        return (f"Net negative: {f.signed_money(net)} USD over {trades} trades, "
                f"{f.money(total_costs, 4)} of that already paid out in costs. "
                f"Check the win rate and the realised R:R before changing the strategy — "
                f"a fee problem and an entry problem look identical on a P&L chart and "
                f"need opposite fixes.",
                "🔴", COLOR_RED)

    # ── Internal send methods ─────────────────────────────────────────────

    def _log(self, payload: dict):
        with open(EVENT_LOG, "a") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")

    def _log_trade(self, action: str, trade_data: dict):
        entry = {
            "action": action,
            "ts": datetime.now(timezone.utc).isoformat(),
            **trade_data,
        }
        with open(TRADE_LOG, "a") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

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
        if event_type not in EMAIL_ALLOWED_EVENTS:
            return
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
        else:
            html = _build_alert_email(event_type, message or (trade_data.get("message", "") if trade_data else ""), priority)

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
