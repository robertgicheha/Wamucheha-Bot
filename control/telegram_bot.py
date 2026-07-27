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
/kill      - Emergency halt all trading
/resume    - Resume trading after review
/help      - List all commands
"""
import os
import json
import logging
import threading

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
        self._app.add_handler(CommandHandler("kill", self._cmd_kill))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
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

        pf = stats.get('profit_factor', 0)
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
            f"  {pf_emoji} Profit Factor:  <code>{pf:.2f}</code>"
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
            "  /hourly — Hourly reports\n\n"
            "<b>Control:</b>\n"
            "  /kill — Emergency halt trading\n"
            "  /resume — Resume trading\n\n"
            "  /help — This message"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    def stop(self):
        if self._app:
            pass
