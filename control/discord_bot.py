"""
Interactive Discord control bot — enhanced with rich embed responses.

Runs as a background thread alongside the main trading engine. Provides
real-time control and monitoring via Discord commands with embed formatting:

!status     - Current risk state, balance, positions
!trades     - Recent closed trades with PnL
!profit     - All-time stats (win rate, PnL, profit factor)
!open       - List open positions with details
!history    - Trade history for a symbol
!performance - Per-symbol performance breakdown
!equity     - Equity curve data
!hourly     - Recent hourly reports
!kill       - Emergency halt all trading
!resume     - Resume trading after review
!help       - List all commands

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

COLOR_GREEN = 0x4CAF50
COLOR_RED = 0xF44336
COLOR_BLUE = 0x2196F3
COLOR_ORANGE = 0xFF9800
COLOR_PURPLE = 0x9C27B0
COLOR_DARK = 0x1A1A2E


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
            halted = bool(rs.get("trading_halted", 0))

            embed = discord.Embed(
                title=f"{'🔴' if halted else '🟢'} Trading Bot Status [{mode}]",
                color=COLOR_RED if halted else COLOR_GREEN,
            )
            embed.add_field(name="Balance", value=f"${rs['trading_balance']:.2f}", inline=True)
            embed.add_field(name="Peak", value=f"${rs.get('peak_balance', 0):.2f}", inline=True)
            embed.add_field(name="Daily PnL", value=f"{rs['daily_pnl']:+.2f} USD", inline=True)
            embed.add_field(name="Consecutive Losses", value=str(rs['consecutive_losses']), inline=True)
            embed.add_field(name="Open Positions", value=str(len(positions)), inline=True)

            if halted:
                embed.add_field(name="🛑 HALTED", value=str(rs.get('halt_reason', 'Unknown')), inline=False)

            if positions:
                pos_text = ""
                for p in positions[:5]:
                    emoji = "🟢" if p["side"] == "buy" else "🔴"
                    pos_text += f"{emoji} `{p['symbol']}` {p['side'].upper()} @ `{p['entry_price']:.5f}`\n"
                if len(positions) > 5:
                    pos_text += f"... and {len(positions) - 5} more"
                embed.add_field(name="Open Positions", value=pos_text, inline=False)

            embed.set_footer(text=f"Last update: {str(rs.get('updated_at', 'N/A'))[:19]}")
            await ctx.reply(embed=embed)

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

            embed = discord.Embed(title="📋 Recent Trades", color=COLOR_BLUE)
            for t in trades:
                if t["status"] == "open":
                    emoji = "🔵"
                    pnl_text = "OPEN"
                else:
                    pnl = t.get("pnl", 0) or 0
                    emoji = "🟢" if pnl > 0 else "🔴"
                    pnl_text = f"{pnl:+.2f} USD"

                embed.add_field(
                    name=f"{emoji} {t['symbol']} {t['side'].upper()}",
                    value=f"PnL: **{pnl_text}**\n{t['exchange']}\n{str(t.get('opened_at', ''))[:19]}",
                    inline=True,
                )
            await ctx.reply(embed=embed)

        @self._bot.command(name="profit")
        async def cmd_profit(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            stats = state.get_all_time_stats()

            pf = stats.get('profit_factor', 0)
            pf_emoji = "🟢" if pf >= 1.5 else "🟡" if pf >= 1 else "🔴"
            color = COLOR_GREEN if (stats.get('net_pnl', 0) or 0) >= 0 else COLOR_RED

            embed = discord.Embed(title="📊 All-Time Performance", color=color)
            embed.add_field(name="Total Trades", value=str(stats.get('total', 0)), inline=True)
            embed.add_field(name="Win Rate", value=f"**{stats.get('win_rate', 0):.1f}%**", inline=True)
            embed.add_field(name="W / L", value=f"{stats.get('wins', 0)} / {stats.get('losses', 0)}", inline=True)
            embed.add_field(name="━━━━━━━━", value="──────────", inline=False)
            embed.add_field(name="Net PnL", value=f"**{stats.get('net_pnl', 0) or 0:+.2f} USD**", inline=True)
            embed.add_field(name="Total Won", value=f"🟢 {stats.get('total_won', 0) or 0:+.2f}", inline=True)
            embed.add_field(name="Total Lost", value=f"🔴 {stats.get('total_lost', 0) or 0:+.2f}", inline=True)
            embed.add_field(name="Avg PnL", value=f"{stats.get('avg_pnl', 0):+.2f}", inline=True)
            embed.add_field(name="Best Trade", value=f"🟢 {stats.get('best_trade', 0):+.2f}", inline=True)
            embed.add_field(name="Worst Trade", value=f"🔴 {stats.get('worst_trade', 0):+.2f}", inline=True)
            embed.add_field(name=f"{pf_emoji} Profit Factor", value=f"{pf:.2f}", inline=True)
            await ctx.reply(embed=embed)

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

            embed = discord.Embed(title="📊 Open Positions", color=COLOR_BLUE)
            for i, p in enumerate(positions, 1):
                emoji = "🟢" if p["side"] == "buy" else "🔴"
                embed.add_field(
                    name=f"{emoji} {p['symbol']} — {p['side'].upper()}",
                    value=(
                        f"Entry: `{p['entry_price']:.5f}`\n"
                        f"Amount: `{p['amount']:.4f}`\n"
                        f"SL: `{p.get('stop_loss_price', 'N/A')}`\n"
                        f"TP: `{p.get('take_profit_price', 'N/A')}`\n"
                        f"Exchange: {p['exchange'].upper()}\n"
                        f"Opened: {str(p.get('opened_at', ''))[:19]}"
                    ),
                    inline=True,
                )
            await ctx.reply(embed=embed)

        @self._bot.command(name="history")
        async def cmd_history(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            symbol = ctx.message.content.split()[1].upper() if len(ctx.message.content.split()) > 1 else None
            if symbol:
                trades = state.get_trades_by_symbol(symbol, 15)
                title = f"History for {symbol}"
            else:
                trades = state.get_recent_trades(15)
                title = "Recent History"

            if not trades:
                await ctx.reply(f"No trades found{' for ' + symbol if symbol else ''}.")
                return

            embed = discord.Embed(title=f"📜 {title}", color=COLOR_BLUE)
            for t in trades:
                pnl = t.get("pnl")
                if t["status"] == "open":
                    emoji = "🔵"
                    pnl_text = "OPEN"
                else:
                    emoji = "🟢" if (pnl or 0) > 0 else "🔴"
                    pnl_text = f"{pnl:+.2f}" if pnl is not None else "N/A"

                strategies = t.get("strategies", [])
                strat_str = f"\n`{', '.join(strategies[:3])}`" if strategies else ""

                embed.add_field(
                    name=f"{emoji} {t['symbol']} {t['side'].upper()}",
                    value=f"PnL: **{pnl_text}**\n{t.get('opened_at', '')[:19]}{strat_str}",
                    inline=True,
                )
            await ctx.reply(embed=embed)

        @self._bot.command(name="performance")
        async def cmd_performance(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            symbol_stats = state.get_symbol_stats()

            if not symbol_stats:
                await ctx.reply("No completed trades yet.")
                return

            sorted_symbols = sorted(symbol_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
            embed = discord.Embed(title="📊 Performance by Symbol", color=COLOR_PURPLE)

            for symbol, s in sorted_symbols[:10]:
                emoji = "🟢" if s["total_pnl"] >= 0 else "🔴"
                embed.add_field(
                    name=f"{emoji} {symbol}",
                    value=(
                        f"Trades: {s['total_trades']} | W/L: {s['wins']}/{s['losses']}\n"
                        f"WR: {s['win_rate']:.0f}% | PnL: `{s['total_pnl']:+.2f}`\n"
                        f"Avg: `{s['avg_pnl']:+.2f}`"
                    ),
                    inline=True,
                )
            await ctx.reply(embed=embed)

        @self._bot.command(name="equity")
        async def cmd_equity(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            state = self._get_state()
            curve = state.get_equity_curve(50)

            if not curve:
                await ctx.reply("No closed trades yet.")
                return

            final = curve[-1]["cumulative_pnl"]
            peak = max(c["cumulative_pnl"] for c in curve)
            trough = min(c["cumulative_pnl"] for c in curve)

            embed = discord.Embed(
                title="📈 Equity Curve",
                color=COLOR_GREEN if final >= 0 else COLOR_RED,
            )
            embed.add_field(name="Current", value=f"`{final:+.2f} USD`", inline=True)
            embed.add_field(name="Peak", value=f"`{peak:+.2f}`", inline=True)
            embed.add_field(name="Trough", value=f"`{trough:+.2f}`", inline=True)
            embed.add_field(name="Trades Tracked", value=str(len(curve)), inline=True)

            last_trades = ""
            for c in curve[-8:]:
                emoji = "🟢" if (c["pnl"] or 0) >= 0 else "🔴"
                last_trades += f"{emoji} {c['symbol']}: `{c['pnl']:+.2f}`\n"
            embed.add_field(name="Last Trades", value=last_trades or "N/A", inline=False)
            await ctx.reply(embed=embed)

        @self._bot.command(name="hourly")
        async def cmd_hourly(ctx):
            if not _is_authorized(ctx):
                await ctx.reply("Unauthorized.")
                return
            from reporting.hourly_report import read_hourly_log
            logs = read_hourly_log(12)

            if not logs:
                await ctx.reply("No hourly reports yet.")
                return

            embed = discord.Embed(title="📊 Hourly Reports (Last 12h)", color=COLOR_ORANGE)
            for r in logs[:8]:
                ts = r.get("ts", "")[:16]
                pnl = r.get("hour_pnl", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                embed.add_field(
                    name=f"{emoji} {ts}Z",
                    value=(
                        f"Trades: {r.get('trades_this_hour', 0)} "
                        f"({r.get('wins_this_hour', 0)}W/{r.get('losses_this_hour', 0)}L)\n"
                        f"PnL: `{pnl:+.2f}` | Bal: `${r.get('trading_balance', 0):.2f}`"
                    ),
                    inline=False,
                )
            await ctx.reply(embed=embed)

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
            embed = discord.Embed(
                title="🛑 TRADING HALTED",
                description="All trading has been stopped.\nUse `!resume` to restart.",
                color=COLOR_RED,
            )
            await ctx.reply(embed=embed)

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
            embed = discord.Embed(
                title="✅ Trading Resumed",
                description="Bot is now actively trading again.",
                color=COLOR_GREEN,
            )
            await ctx.reply(embed=embed)

        @self._bot.command(name="help")
        async def cmd_help(ctx):
            embed = discord.Embed(
                title="🤖 Wamucheha Trading Bot",
                description="Automated multi-asset trading bot",
                color=COLOR_BLUE,
            )
            embed.add_field(
                name="📊 Monitoring",
                value=(
                    "`!status` — Bot status & balance\n"
                    "`!trades` — Recent trades\n"
                    "`!profit` — All-time PnL stats\n"
                    "`!open` — Open positions\n"
                    "`!history [SYMBOL]` — Trade history\n"
                    "`!performance` — Per-symbol breakdown\n"
                    "`!equity` — Equity curve\n"
                    "`!hourly` — Hourly reports"
                ),
                inline=False,
            )
            embed.add_field(
                name="🎛️ Control",
                value=(
                    "`!kill` — Emergency halt trading\n"
                    "`!resume` — Resume trading"
                ),
                inline=False,
            )
            await ctx.reply(embed=embed)

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
            pass
