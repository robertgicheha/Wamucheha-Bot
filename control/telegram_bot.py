"""
Interactive Telegram control bot — enhanced with rich formatted responses.

Runs as a background thread alongside the main trading engine. Provides
real-time control and monitoring via Telegram commands with HTML formatting:

/status    - Current risk state, balance, positions (styled)
/trades    - Recent closed trades with PnL colors
/profit    - All-time stats (win rate, PnL, profit factor)
/open      - List open positions with details
/history   - Full trade history for a symbol
/performance - Per-symbol performance breakdown
/equity    - Equity curve data
/hourly    - Recent hourly reports
/digest    - Last 5-minute trade digests
/cash      - Funded vs returned vs profit, kept separate
/deposit   - Log capital moved into a venue
/withdraw  - Log capital taken out of a venue
/kill      - Emergency halt all trading
/resume    - Resume trading after review
/venues    - Per-venue exposure and kill-switch state
/disable_venue - Block new entries on one venue
/enable_venue  - Allow entries on a venue again
/help      - List all commands
"""
import os
import json
import html
import logging
import threading

from alerts import formatting as f

try:
    from telegram import Update
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, filters, ContextTypes,
    )
    _has_telegram = True
except ImportError:
    _has_telegram = False

logger = logging.getLogger("telegram_bot")

DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET_KEY", "change_me")
ALLOWED_USERS = os.environ.get("TELEGRAM_ALLOWED_USERS", "")


def _is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    allowed = [int(uid.strip()) for uid in ALLOWED_USERS.split(",") if uid.strip()]
    return user_id in allowed


def _esc_html(value) -> str:
    """Telegram sends these as parse_mode=HTML, so operator-supplied arguments
    (venue names, reasons) must be escaped before they are interpolated."""
    return html.escape(str(value), quote=False)


def _parse_amount(args) -> tuple:
    """('/deposit', '250', 'binance') -> (250.0, 'binance').

    Returns (0.0, '') on anything unusable, including a negative amount: this
    writes to a ledger, and a mistyped sign there is a lie that outlives the
    command."""
    if not args:
        return 0.0, ""
    try:
        amount = float(str(args[0]).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return 0.0, ""
    if amount <= 0:
        return 0.0, ""
    venue = _esc_html(args[1]).lower() if len(args) > 1 else ""
    return amount, venue


def _pnl_color(pnl: float) -> str:
    return "🟢" if pnl >= 0 else "🔴"


def _status_emoji(halted: bool) -> str:
    return "🔴" if halted else "🟢"


class TelegramControlBot:
    def __init__(self, state_manager=None, risk_manager=None):
        self.state = state_manager
        self.risk = risk_manager
        self._app = None
        self._thread = None

    def start(self, token: str):
        if not _has_telegram:
            logger.warning("python-telegram-bot not installed. Control bot disabled.")
            return

        self._app = Application.builder().token(token).build()

        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("profit", self._cmd_profit))
        self._app.add_handler(CommandHandler("open", self._cmd_open))
        self._app.add_handler(CommandHandler("history", self._cmd_history))
        self._app.add_handler(CommandHandler("performance", self._cmd_performance))
        self._app.add_handler(CommandHandler("equity", self._cmd_equity))
        self._app.add_handler(CommandHandler("hourly", self._cmd_hourly))
        self._app.add_handler(CommandHandler("deposit", self._cmd_deposit))
        self._app.add_handler(CommandHandler("withdraw", self._cmd_withdraw))
        self._app.add_handler(CommandHandler("cash", self._cmd_cash))
        self._app.add_handler(CommandHandler("digest", self._cmd_digest))
        self._app.add_handler(CommandHandler("kill", self._cmd_kill))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("venues", self._cmd_venues))
        self._app.add_handler(CommandHandler("disable_venue", self._cmd_disable_venue))
        self._app.add_handler(CommandHandler("enable_venue", self._cmd_enable_venue))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

        self._thread = threading.Thread(target=self._run_polling, daemon=True)
        self._thread.start()
        logger.info("Telegram control bot started (polling mode)")

    def _run_polling(self):
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._app.run_polling(drop_pending_updates=True, stop_signals=None))

    def _get_state(self):
        if self.state is None:
            from core.state_manager import StateManager
            self.state = StateManager(stake_amount=0)
        return self.state

    def _get_risk(self):
        """The risk manager is in-process, so the venue kill switch it holds is
        the one the engine consults on every order. A second instance would be
        a different set with no effect on trading, so fall back to nothing
        rather than constructing a detached one."""
        return self.risk

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        rs = state.get_risk_state()
        positions = state.get_open_positions()
        mode = "LIVE" if os.environ.get("LIVE_TRADING", "false").lower() == "true" else "DRY-RUN"
        halted = bool(rs.get("trading_halted", 0))

        msg = (
            f"<b>{'🔴' if halted else '🟢'} Trading Bot Status</b> [{mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>💰 Account</b>\n"
            f"  Balance:  <code>${rs['trading_balance']:.2f}</code>\n"
            f"  Peak:  <code>${rs.get('peak_balance', 0):.2f}</code>\n"
            f"  Daily PnL:  <code>{rs['daily_pnl']:+.2f} USD</code>\n\n"
            f"<b>⚠️ Risk</b>\n"
            f"  Consecutive Losses:  {rs['consecutive_losses']}/8\n"
            f"  Open Positions:  {len(positions)}\n\n"
        )

        if halted:
            msg += f"<b>🛑 HALTED:</b> <i>{rs.get('halt_reason', 'Unknown')}</i>\n\n"

        # Show open positions summary
        if positions:
            msg += "<b>📊 Open Positions:</b>\n"
            for p in positions[:5]:
                emoji = "🟢" if p["side"] == "buy" else "🔴"
                msg += f"  {emoji} <code>{p['symbol']}</code> {p['side'].upper()} @ {p['entry_price']:.5f}\n"
            if len(positions) > 5:
                msg += f"  ... and {len(positions) - 5} more\n"

        msg += f"\n<i>Last update: {rs.get('updated_at', 'N/A')[:19]}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        trades = state.get_recent_trades(10)
        if not trades:
            await update.message.reply_text("No trades yet.")
            return

        msg = "<b>📋 Recent Trades</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for t in trades:
            if t["status"] == "open":
                emoji = "🔵"
                pnl_text = "OPEN"
            else:
                pnl = t.get("pnl", 0) or 0
                emoji = "🟢" if pnl > 0 else "🔴"
                pnl_text = f"{pnl:+.2f} USD"

            msg += (
                f"{emoji} <code>{t['symbol']}</code> {t['side'].upper()}\n"
                f"   PnL: <b>{pnl_text}</b> | {t['exchange']}\n"
                f"   <i>{t.get('opened_at', '')[:19]}</i>\n\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_profit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        stats = state.get_all_time_stats()

        # Net of execution costs, because that is the number that decides
        # whether to keep running. The gross ratio is shown beside it so the
        # gap between them is visible — that gap is what the fees cost.
        pf = stats.get('profit_factor_net', 0)
        pf_gross = stats.get('profit_factor', 0)
        pf_emoji = "🟢" if pf >= 1.5 else "🟡" if pf >= 1 else "🔴"

        msg = (
            f"<b>📊 All-Time Performance</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>📈 Summary</b>\n"
            f"  Total Trades:  <b>{stats.get('total', 0)}</b>\n"
            f"  Win Rate:  <b>{stats.get('win_rate', 0):.1f}%</b>\n"
            f"  W / L:  <b>{stats.get('wins', 0)} / {stats.get('losses', 0)}</b>\n\n"
            f"<b>💰 PnL</b>\n"
            f"  Net PnL:  <code>{_pnl_color(stats.get('net_pnl', 0))} {stats.get('net_pnl', 0) or 0:+.2f} USD</code>\n"
            f"  Total Won:  <code>🟢 {stats.get('total_won', 0) or 0:+.2f}</code>\n"
            f"  Total Lost:  <code>🔴 {stats.get('total_lost', 0) or 0:+.2f}</code>\n"
            f"  Avg PnL:  <code>{stats.get('avg_pnl', 0):+.2f}</code>\n\n"
            f"<b>⚡ Extremes</b>\n"
            f"  Best Trade:  <code>🟢 {stats.get('best_trade', 0):+.2f}</code>\n"
            f"  Worst Trade:  <code>🔴 {stats.get('worst_trade', 0):+.2f}</code>\n"
            f"  {pf_emoji} Profit Factor:  <code>{pf:.2f}</code> <i>net of costs</i>"
            f"\n  <i>before costs: {pf_gross:.2f} — the gap is what fees cost</i>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        positions = state.get_open_positions()
        if not positions:
            await update.message.reply_text("No open positions.")
            return

        msg = "<b>📊 Open Positions</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for i, p in enumerate(positions, 1):
            emoji = "🟢" if p["side"] == "buy" else "🔴"
            msg += (
                f"<b>{i}. {emoji} {p['symbol']}</b>\n"
                f"   Side: <b>{p['side'].upper()}</b>\n"
                f"   Entry: <code>{p['entry_price']:.5f}</code>\n"
                f"   Amount: <code>{p['amount']:.4f}</code>\n"
                f"   SL: <code>{p.get('stop_loss_price', 'N/A')}</code>\n"
                f"   TP: <code>{p.get('take_profit_price', 'N/A')}</code>\n"
                f"   Exchange: {p['exchange'].upper()}\n"
                f"   <i>Opened: {p.get('opened_at', '')[:19]}</i>\n\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        # Parse optional symbol from args
        symbol = context.args[0] if context.args else None
        if symbol:
            trades = state.get_trades_by_symbol(symbol.upper(), 15)
            title = f"History for {symbol.upper()}"
        else:
            trades = state.get_recent_trades(15)
            title = "Recent History"

        if not trades:
            await update.message.reply_text(f"No trades found{' for ' + symbol.upper() if symbol else ''}.")
            return

        msg = f"<b>📜 {title}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for t in trades:
            pnl = t.get("pnl")
            if t["status"] == "open":
                emoji = "🔵"
                pnl_text = "OPEN"
            else:
                emoji = "🟢" if (pnl or 0) > 0 else "🔴"
                pnl_text = f"{pnl:+.2f}" if pnl is not None else "N/A"

            strategies = t.get("strategies", [])
            strat_str = f" [{', '.join(strategies[:3])}]" if strategies else ""

            msg += (
                f"{emoji} <code>{t['symbol']}</code> {t['side'].upper()} | "
                f"PnL: <b>{pnl_text}</b>{strat_str}\n"
                f"   <i>{t.get('opened_at', '')[:19]} → {t.get('closed_at', 'open')[:19] if t.get('closed_at') else 'open'}</i>\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        symbol_stats = state.get_symbol_stats()

        if not symbol_stats:
            await update.message.reply_text("No completed trades yet.")
            return

        # Sort by total PnL descending
        sorted_symbols = sorted(symbol_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True)

        msg = "<b>📊 Performance by Symbol</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for symbol, s in sorted_symbols[:15]:
            emoji = "🟢" if s["total_pnl"] >= 0 else "🔴"
            msg += (
                f"{emoji} <b>{symbol}</b>\n"
                f"   Trades: {s['total_trades']} | "
                f"W/L: {s['wins']}/{s['losses']} | "
                f"WR: {s['win_rate']:.0f}%\n"
                f"   PnL: <code>{s['total_pnl']:+.2f}</code> | "
                f"Avg: <code>{s['avg_pnl']:+.2f}</code>\n\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_equity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        curve = state.get_equity_curve(50)

        if not curve:
            await update.message.reply_text("No closed trades yet for equity curve.")
            return

        final = curve[-1]["cumulative_pnl"]
        peak = max(c["cumulative_pnl"] for c in curve)
        trough = min(c["cumulative_pnl"] for c in curve)

        msg = (
            f"<b>📈 Equity Curve</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  Trades tracked: {len(curve)}\n"
            f"  Current: <code>{final:+.2f} USD</code>\n"
            f"  Peak: <code>{peak:+.2f}</code>\n"
            f"  Trough: <code>{trough:+.2f}</code>\n"
            f"  Max DD: <code>{peak - trough:+.2f}</code>\n\n"
            f"<b>Last 10 trades:</b>\n"
        )
        for c in curve[-10:]:
            emoji = "🟢" if (c["pnl"] or 0) >= 0 else "🔴"
            msg += f"  {emoji} {c['symbol']} | {c['pnl']:+.2f} | Cum: {c['cumulative_pnl']:+.2f}\n"

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_hourly(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        from reporting.hourly_report import read_hourly_log
        logs = read_hourly_log(12)

        if not logs:
            await update.message.reply_text("No hourly reports yet.")
            return

        msg = "<b>📊 Hourly Reports (Last 12h)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for r in logs:
            ts = r.get("ts", "")[:16]
            pnl = r.get("hour_pnl", 0)
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg += (
                f"{emoji} <code>{ts}Z</code>\n"
                f"   Trades: {r.get('trades_this_hour', 0)} "
                f"({r.get('wins_this_hour', 0)}W/{r.get('losses_this_hour', 0)}L) | "
                f"PnL: <code>{pnl:+.2f}</code> | "
                f"Bal: <code>${r.get('trading_balance', 0):.2f}</code>\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")

    # ---------- capital in / out ----------
    # The bot never moves money, so these commands are how the ledger learns
    # that it did. Without them a deposit is indistinguishable from a run of
    # winning trades in every balance-based number the bot reports.

    async def _cmd_deposit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        parsed = _parse_amount(context.args)
        if not parsed:
            await update.message.reply_text(
                "Usage: <code>/deposit 250</code> or <code>/deposit 250 binance</code>")
            return
        amount, venue = parsed
        state = self._get_state()
        state.record_cash_flow("deposit", amount, venue=venue,
                               note=f"declared via Telegram by {update.effective_user.id}")
        await update.message.reply_text(
            f"📥 <b>DEPOSIT LOGGED</b>\n\n"
            f"<code>+{amount:,.2f} USD</code> ➜ {venue or 'the account'}\n\n"
            f"<i>Counted as capital in, not profit. Use /cash to see the split.</i>",
            parse_mode="HTML")

    async def _cmd_withdraw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        parsed = _parse_amount(context.args)
        if not parsed:
            await update.message.reply_text(
                "Usage: <code>/withdraw 250</code> or <code>/withdraw 250 binance</code>")
            return
        amount, venue = parsed
        state = self._get_state()
        state.record_cash_flow("withdrawal", amount, venue=venue,
                               note=f"declared via Telegram by {update.effective_user.id}")
        await update.message.reply_text(
            f"📤 <b>WITHDRAWAL LOGGED</b>\n\n"
            f"<code>−{amount:,.2f} USD</code> ➜ {venue or 'from the account'}\n\n"
            f"<i>Your capital leaving, not a loss. The bot does not move funds — "
            f"this records what you did.</i>",
            parse_mode="HTML")

    async def _cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The 5-minute digests, most recent first. The same message the channel
        received, so asking the bot what it said and trusting what it said are
        the same thing."""
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        from reporting.trade_digest import read_digests
        digests = read_digests(6)

        if not digests:
            await update.message.reply_text(
                "🕐 <b>No digests yet.</b>\n\n"
                "<i>A digest is only written when a trade closes. Silence here "
                "means no trades have closed yet — check /status for the bot's "
                "health.</i>", parse_mode="HTML")
            return

        for d in digests:
            net = float(d.get("net_pnl", 0.0))
            # Absent means the digest could not reconstruct the window start,
            # which is different from a start of zero.
            bal_before = d.get("balance_before")
            bal_before = float(bal_before) if bal_before is not None else None
            bal_now = float(d.get("balance_now", 0.0))
            icon = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
            classes = sorted({f.asset_class(t["symbol"])
                              for t in (d.get("trades") or [])})
            balance_text = (f"   💼 {bal_before:,.2f} ➜ {bal_now:,.2f} USD"
                            if bal_before is not None else
                            f"   💼 {bal_now:,.2f} USD"
                            f"  <i>(window start not derivable)</i>")
            lines = [
                f"{icon} <b>{d.get('ts', '')[:16].replace('T', ' ')}Z</b>  "
                f"<code>{d.get('trade_count', 0)}</code> trade(s)  "
                f"net <code>{net:+.2f} USD</code>",
                balance_text +
                f"  ·  ⛽ {float(d.get('fees', 0)):4f} cost",
            ]
            for t in (d.get("trades") or []):
                lines.append(
                    f"   {'🟢' if t['pnl'] > 0 else '🔴'} {t['symbol']} "
                    f"{'LONG' if t['side'] == 'buy' else 'SHORT'} · "
                    f"{f.venue_name(t['exchange'])} · "
                    f"{t['entry_price']} ➜ {t['exit_price']} · "
                    f"<code>{t['pnl']:+.2f}</code>")
            if classes:
                lines.append(f"   <i>classes: {', '.join(classes)}</i>")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_cash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The three numbers that must never be added together: what you put
        in, what the bot earned, and what you took back out."""
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        cash = state.get_cash_flow_totals()
        risk_state = state.get_risk_state()
        stats = state.get_all_time_stats()

        arrow = lambda v: "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")
        msg = [
            "<b>🏦 CAPITAL vs PROFIT</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📥 <b>Funded</b>      <code>{cash['deposited']:,.2f} USD</code>",
            f"📤 <b>Returned</b>    <code>{cash['returned']:,.2f} USD</code>"
            f"  <i>your capital, not profit</i>",
            f"🏦 <b>Profit swept</b>  <code>{cash['swept']:,.2f} USD</code>"
            f"  <i>earned, moved to safety</i>",
            f"{arrow(cash['profit'])} <b>Profit earned</b> <code>{cash['profit']:+,.2f} USD</code>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💼 <b>Trading balance now</b>  <code>{risk_state.get('trading_balance', 0):,.2f} USD</code>",
            f"💎 <b>All-time net</b>        <code>{stats.get('net_pnl', 0):+,.2f} USD</code>"
            f"  <i>· {stats.get('total', 0)} trades · {stats.get('win_rate', 0):.0f}% WR</i>",
        ]
        if cash["by_venue"]:
            msg.append("━━━━━━━━━━━━━━━━━━━━━━")
            msg.append("🏦 <b>Per venue</b>")
            for venue, amounts in cash["by_venue"].items():
                if not any(amounts.values()):
                    continue
                msg.append(
                    f"   <code>{venue}</code>  in <code>{amounts['deposit']:,.2f}</code> · "
                    f"out <code>{amounts['withdrawal'] + amounts['sweep']:,.2f}</code>")
        await update.message.reply_text("\n".join(msg), parse_mode="HTML")

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        state.update_risk_state(
            trading_halted=1,
            halt_reason=f"Manual kill via Telegram by user {update.effective_user.id}",
        )
        await update.message.reply_text(
            "<b>🛑 TRADING HALTED</b>\n\nAll trading has been stopped.\nUse /resume to restart.",
            parse_mode="HTML",
        )

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        state.update_risk_state(
            trading_halted=0,
            halt_reason=None,
            consecutive_losses=0,
        )
        await update.message.reply_text(
            "<b>✅ Trading Resumed</b>\n\nBot is now actively trading again.",
            parse_mode="HTML",
        )

    # ---------- per-venue kill switch ----------

    def _venue_exposure(self) -> dict:
        """Open notional per venue, using the risk manager's own notional
        conversion so a reported figure matches what the cap is measured on."""
        from core.risk_manager import position_notional_usd

        out = {}
        for p in self._get_state().get_open_positions():
            venue = (p.get("exchange") or "unknown").lower()
            out[venue] = out.get(venue, 0.0) + position_notional_usd(p)
        return out

    async def _cmd_venues(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        risk = self._get_risk()
        rs = self._get_state().get_risk_state()
        balance = rs.get("trading_balance", 0) or 0
        if risk is None:
            await update.message.reply_text(
                "Risk manager unavailable in this process — venue kill switch state unknown.",
                parse_mode="HTML")
            return
        cap = risk.max_venue_exposure_pct
        disabled = set(risk.get_disabled_venues())
        exposure = self._venue_exposure()

        msg = ("<b>🏦 Venues</b>\n"
               "<i>Each venue holds its own balance and deposit address.\n"
               "Open notional vs cap:</i>\n\n")
        if not exposure:
            msg += "  no open positions\n"
        for venue, value in sorted(exposure.items()):
            pct = value / balance * 100 if balance > 0 else 0
            msg += f"  <b>{venue}</b> — ${value:,.2f} ({pct:.1f}% of {cap}% cap)\n"
        if disabled:
            msg += f"\n🔴 <b>BLOCKED:</b> {', '.join(sorted(disabled))}\n"
        msg += ("\n/disable_venue binance — block new entries on a venue\n"
                "/enable_venue binance — allow again\n"
                "<i>Open positions are still closed normally.</i>")
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_disable_venue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/disable_venue binance</code>", parse_mode="HTML")
            return
        venue = context.args[0].strip().lower()
        reason = " ".join(context.args[1:]) or "no reason given"
        if self._get_risk() is None:
            await update.message.reply_text(
                "Risk manager unavailable — cannot change the kill switch.", parse_mode="HTML")
            return
        self._get_risk().disable_venue(venue, reason, actor=f"TG user {update.effective_user.id}")
        await update.message.reply_text(
            f"🔴 New entries on <b>{_esc_html(venue)}</b> are now blocked.\n"
            f"Open positions there are still managed and closed normally.\n"
            f"Re-enable with <code>/enable_venue {_esc_html(venue)}</code>.",
            parse_mode="HTML")

    async def _cmd_enable_venue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/enable_venue binance</code>", parse_mode="HTML")
            return
        venue = context.args[0].strip().lower()
        if self._get_risk() is None:
            await update.message.reply_text(
                "Risk manager unavailable — cannot change the kill switch.", parse_mode="HTML")
            return
        self._get_risk().enable_venue(venue, actor=f"TG user {update.effective_user.id}")
        await update.message.reply_text(
            f"🟢 New entries on <b>{_esc_html(venue)}</b> re-enabled.",
            parse_mode="HTML")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = (
            "<b>🤖 Wamucheha Trading Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Monitoring:</b>\n"
            "  /status — Bot status & balance\n"
            "  /trades — Recent trades\n"
            "  /profit — All-time PnL stats\n"
            "  /open — Open positions\n"
            "  /history [SYMBOL] — Trade history\n"
            "  /performance — Per-symbol breakdown\n"
            "  /equity — Equity curve\n"
            "  /hourly — Hourly reports\n"
            "  /digest — Last 5-minute trade digests\n"
            "  /cash — Funded vs returned vs profit\n"
            "  /venues — Per-venue exposure & kill switch state\n\n"
            "<b>Control:</b>\n"
            "  /kill — Emergency halt trading\n"
            "  /resume — Resume trading\n"
            "  /disable_venue [venue] [reason] — block new entries on one venue\n"
            "  /enable_venue [venue] — allow entries again\n"
            "  /deposit [amount] [venue] — log capital you moved in\n"
            "  /withdraw [amount] [venue] — log capital you took out\n\n"
            "  /help — This message"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    def stop(self):
        if self._app:
            pass
