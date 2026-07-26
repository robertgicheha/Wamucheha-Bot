"""
Interactive Telegram control bot using python-telegram-bot.

Runs as a background thread alongside the main trading engine. Provides
real-time control and monitoring via Telegram commands:

/status   - Current risk state, balance, positions
/trades   - Recent closed trades
/profit   - All-time stats (win rate, PnL)
/open     - List open positions
/kill     - Emergency halt all trading
/resume   - Resume trading after review
/help     - List all commands

The bot uses polling (not webhooks) so it works without a public URL.
"""
import os
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
ALLOWED_USERS = os.environ.get("TELEGRAM_ALLOWED_USERS", "")  # comma-separated user IDs


def _is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # no restriction if not configured
    allowed = [int(uid.strip()) for uid in ALLOWED_USERS.split(",") if uid.strip()]
    return user_id in allowed


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
        loop.run_until_complete(self._app.run_polling(drop_pending_updates=True))

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
        msg = (
            f"🤖 Trading Bot Status ({mode})\n\n"
            f"Balance: ${rs['trading_balance']:.2f}\n"
            f"Peak: ${rs.get('peak_balance', 0):.2f}\n"
            f"Daily PnL: ${rs['daily_pnl']:.2f}\n"
            f"Consecutive losses: {rs['consecutive_losses']}\n"
            f"Open positions: {len(positions)}\n"
            f"Status: {'HALTED - ' + str(rs.get('halt_reason', '')) if rs['trading_halted'] else 'ACTIVE'}\n"
            f"Last update: {rs.get('updated_at', 'N/A')}"
        )
        await update.message.reply_text(msg)

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        trades = state.get_recent_trades(10)
        if not trades:
            await update.message.reply_text("No trades yet.")
            return
        lines = ["Recent Trades:\n"]
        for t in trades:
            pnl_str = f"${t['pnl']:.2f}" if t['pnl'] is not None else "open"
            lines.append(
                f"[{t['status']}] {t['side'].upper()} {t['symbol']} "
                f"PnL={pnl_str} ({t['exchange']})"
            )
        await update.message.reply_text("\n".join(lines))

    async def _cmd_profit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        stats = state.get_all_time_stats()
        msg = (
            f"📊 All-Time Stats\n\n"
            f"Total trades: {stats.get('total', 0)}\n"
            f"Win rate: {stats.get('win_rate', 0):.1f}%\n"
            f"Wins: {stats.get('wins', 0)}\n"
            f"Losses: {stats.get('losses', 0)}\n"
            f"Total won: ${stats.get('total_won', 0) or 0:.2f}\n"
            f"Total lost: ${stats.get('total_lost', 0) or 0:.2f}\n"
            f"Net PnL: ${stats.get('net_pnl', 0) or 0:.2f}"
        )
        await update.message.reply_text(msg)

    async def _cmd_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        positions = state.get_open_positions()
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        lines = ["Open Positions:\n"]
        for p in positions:
            lines.append(
                f"{p['side'].upper()} {p['symbol']} "
                f"@ {p['entry_price']:.5f} ({p['exchange']})\n"
                f"  SL: {p.get('stop_loss_price', 'N/A')} | TP: {p.get('take_profit_price', 'N/A')}"
            )
        await update.message.reply_text("\n".join(lines))

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        state = self._get_state()
        state.update_risk_state(
            trading_halted=1,
            halt_reason=f"Manual kill via Telegram by user {update.effective_user.id}",
        )
        await update.message.reply_text("🛑 TRADING HALTED. Use /resume to restart.")

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
        await update.message.reply_text("✅ Trading resumed.")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = (
            "Commands:\n"
            "/status - Bot status & balance\n"
            "/trades - Recent trades\n"
            "/profit - All-time PnL stats\n"
            "/open - Open positions\n"
            "/kill - Emergency halt trading\n"
            "/resume - Resume trading\n"
            "/help - This message"
        )
        await update.message.reply_text(msg)

    def stop(self):
        if self._app:
            pass  # polling stops when daemon thread exits
