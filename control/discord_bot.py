"""
Interactive Discord control bot using discord.py.

Runs as a background thread alongside the main trading engine. Provides
real-time control and monitoring via Discord slash commands or prefix commands:

!status   - Current risk state, balance, positions
!trades   - Recent closed trades
!profit   - All-time stats (win rate, PnL)
!open     - List open positions
!kill     - Emergency halt all trading
!resume   - Resume trading after review
!help     - List all commands

Requires DISCORD_BOT_TOKEN in .env (separate from the webhook URL used for alerts).
"""
import os
import logging
import threading

try:
    import discord
    from discord.ext import commands
    _has_discord = True
except ImportError:
    _has_discord = False

logger = logging.getLogger("discord_bot")

ALLOWED_ROLES = os.environ.get("DISCORD_ALLOWED_ROLES", "")


def _is_authorized(ctx) -> bool:
    if not ALLOWED_ROLES:
        return True
    allowed = [r.strip().lower() for r in ALLOWED_ROLES.split(",") if r.strip()]
    return any(role.name.lower() in allowed for role in ctx.author.roles)


class DiscordControlBot:
    def __init__(self, state_manager=None, risk_manager=None):
        self.state = state_manager
        self.risk = risk_manager
        self._bot = None
        self._thread = None

    def start(self, token: str):
        if not _has_discord:
            logger.warning("discord.py not installed. Control bot disabled.")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)

        @self._bot.event
        async def on_ready():
            logger.info(f"Discord control bot logged in as {self._bot.user}")

        @self._bot.command(name="status")
        async def cmd_status(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            rs = state.get_risk_state()
            positions = state.get_open_positions()
            mode = "LIVE" if os.environ.get("LIVE_TRADING", "false").lower() == "true" else "DRY-RUN"
            msg = (
                f"**Trading Bot Status** ({mode})\n\n"
                f"Balance: **${rs['trading_balance']:.2f}**\n"
                f"Peak: ${rs.get('peak_balance', 0):.2f}\n"
                f"Daily PnL: ${rs['daily_pnl']:.2f}\n"
                f"Consecutive losses: {rs['consecutive_losses']}\n"
                f"Open positions: {len(positions)}\n"
                f"Status: {'**HALTED** - ' + str(rs.get('halt_reason', '')) if rs['trading_halted'] else '**ACTIVE**'}"
            )
            await ctx.reply(msg)

        @self._bot.command(name="trades")
        async def cmd_trades(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            trades = state.get_recent_trades(10)
            if not trades:
                await ctx.reply("No trades yet.")
                return
            lines = ["**Recent Trades:**\n"]
            for t in trades:
                pnl_str = f"${t['pnl']:.2f}" if t['pnl'] is not None else "open"
                lines.append(
                    f"`[{t['status']}]` {t['side'].upper()} **{t['symbol']}** "
                    f"PnL={pnl_str} ({t['exchange']})"
                )
            await ctx.reply("\n".join(lines))

        @self._bot.command(name="profit")
        async def cmd_profit(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            stats = state.get_all_time_stats()
            msg = (
                f"**All-Time Stats**\n\n"
                f"Total trades: {stats.get('total', 0)}\n"
                f"Win rate: **{stats.get('win_rate', 0):.1f}%**\n"
                f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
                f"Total won: ${stats.get('total_won', 0) or 0:.2f}\n"
                f"Total lost: ${stats.get('total_lost', 0) or 0:.2f}\n"
                f"Net PnL: **${stats.get('net_pnl', 0) or 0:.2f}**"
            )
            await ctx.reply(msg)

        @self._bot.command(name="open")
        async def cmd_open(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            positions = state.get_open_positions()
            if not positions:
                await ctx.reply("No open positions.")
                return
            lines = ["**Open Positions:**\n"]
            for p in positions:
                lines.append(
                    f"{p['side'].upper()} **{p['symbol']}** "
                    f"@ `{p['entry_price']:.5f}` ({p['exchange']})\n"
                    f"SL: {p.get('stop_loss_price', 'N/A')} | TP: {p.get('take_profit_price', 'N/A')}"
                )
            await ctx.reply("\n".join(lines))

        @self._bot.command(name="kill")
        async def cmd_kill(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            state.update_risk_state(
                trading_halted=1,
                halt_reason=f"Manual kill via Discord by {ctx.author}",
            )
            await ctx.reply("🛑 **TRADING HALTED.** Use `!resume` to restart.")

        @self._bot.command(name="resume")
        async def cmd_resume(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            state.update_risk_state(
                trading_halted=0,
                halt_reason=None,
                consecutive_losses=0,
            )
            await ctx.reply("✅ **Trading resumed.**")

        @self._bot.command(name="help")
        async def cmd_help(ctx):
            msg = (
                "**Commands:**\n"
                "`!status` - Bot status & balance\n"
                "`!trades` - Recent trades\n"
                "`!profit` - All-time PnL stats\n"
                "`!open` - Open positions\n"
                "`!kill` - Emergency halt trading\n"
                "`!resume` - Resume trading\n"
                "`!help` - This message"
            )
            await ctx.reply(msg)

        self._thread = threading.Thread(target=self._run_bot, daemon=True)
        self._thread.start()
        logger.info("Discord control bot started")

    def _run_bot(self):
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if token:
            loop.run_until_complete(self._bot.start(token))

    def _get_state(self):
        if self.state is None:
            from core.state_manager import StateManager
            self.state = StateManager(stake_amount=0)
        return self.state

    def stop(self):
        if self._bot:
            pass  # daemon thread exits on process termination
