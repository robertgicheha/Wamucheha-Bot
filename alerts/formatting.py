"""
Display vocabulary for every message the bot sends.

One place decides what a profit looks like, what a loss looks like, how a
price moves on screen and how money is rendered — so the Telegram alert, the
Discord embed, the 6-hour report and the 24-hour report cannot drift apart
and start contradicting each other.

The arrows are the point. ▲ and ▼ carry the direction before you have read a
single digit, which is what makes a phone notification scannable at a glance
instead of something you have to parse. A loss is never dressed up as a
neutral number: it is ▼, red, and prefixed with a minus.
"""
from datetime import datetime, timezone

# ── Direction ────────────────────────────────────────────────────────────
UP = "▲"        # ▲ gained
DOWN = "▼"      # ▼ lost
FLAT = "▬"     # ▬ nothing moved
ARROW_RIGHT = "➜"   # from -> to
ARROW_UP = "↗"
ARROW_DOWN = "↘"
BULLET = "•"

# ── Asset class ──────────────────────────────────────────────────────────
# Every log line says what KIND of market produced the result. "ETH/USDT made
# 4.10" and "XAUUSD made 4.10" are not the same fact, and a reader cannot
# tell from a symbol alone which rules, which session hours and which margin
# regime applied. Naming the class is what makes a P&L line interpretable.
E_CRYPTO = "🪙"
E_FOREX = "💱"
E_METAL = "🥇"
E_EQUITY = "🏛"
E_BOND = "📜"
E_NSE = "🇰🇪"
E_OTHER = "🧩"

ASSET_CLASS_META = {
    "crypto":         (E_CRYPTO, "CRYPTO",      "spot crypto, 24/7"),
    "forex":          (E_FOREX, "FOREX",       "currency pairs"),
    "commodities":    (E_METAL, "GOLD/METALS", "precious metals"),
    "equities":       (E_EQUITY, "EQUITIES",   "US stocks & ETFs"),
    "fixed_income":   (E_BOND,  "BONDS",       "treasuries & ETFs"),
    "nse":            (E_NSE,   "NSE",         "Kenyan equities"),
}
UNKNOWN_CLASS = (E_OTHER, "OTHER", "")

# How many trades in one window get the full multi-line treatment. Past this,
# the rest collapse to a single attributed line each. Sized so a full digest
# stays inside Telegram's 4096-character message limit with the rollup
# sections below it, which is what fails first when a window is busy. The
# sender splits anything longer as a backstop, but a split digest arrives as
# two notifications to dismiss, so the renderer is kept short on purpose.
MAX_DETAILED_TRADES = 3

# ── Venue ────────────────────────────────────────────────────────────────
# A number is only attributable to a venue if the venue is named. "Profitable
# on 6 venues" and "profitable because one venue paid out" are the same log
# line until the venue is attached to it.
VENUE_META = {
    "binance": "Binance",
    "okx":     "OKX",
    "bybit":   "Bybit",
    "kraken":  "Kraken",
    "oanda":   "OANDA",
    "alpaca":  "Alpaca",
    "mt5":     "MetaTrader 5",
    "paper":   "Paper",
}
# Venues whose balances are not USDT in a hot wallet — the operator funds
# these through a broker dashboard, not a chain transfer. Worth saying in a
# log line so nobody waits for an on-chain deposit that was never coming.
BROKER_VENUES = {"mt5", "oanda", "alpaca"}


def venue_name(venue: str) -> str:
    v = (venue or "").strip().lower()
    if not v:
        return "Unknown venue"
    return VENUE_META.get(v, v.upper())


def asset_class(symbol: str) -> str:
    """Classify a symbol. Prefers the risk manager's canonical map so a log
    line and a risk cap can never disagree about what a market is."""
    from core.risk_manager import ASSET_CLASS_MAP
    sym = (symbol or "").strip()
    if not sym:
        return "other"
    if sym in ASSET_CLASS_MAP:
        return ASSET_CLASS_MAP[sym]
    upper = sym.upper()
    for candidate, cls in ASSET_CLASS_MAP.items():
        if candidate.upper() == upper:
            return cls
    # Not in the configured map. Fall back on shape so an unlisted market is
    # still described honestly rather than silently bucketed as crypto.
    if "XAU" in upper or "XAG" in upper or "GLD" in upper:
        return "commodities"
    if "/" in sym:
        base, _, quote = sym.partition("/")
        if quote.upper() in {"USDT", "USDC", "BUSD", "FDUSD", "USD", "EUR", "GBP"}:
            # USDT/USDC/BUSD quote a coin; USD/EUR/GBP quote a currency.
            if quote.upper() in {"USDT", "USDC", "BUSD", "FDUSD"}:
                return "crypto"
            return "forex" if len(base) <= 4 else "crypto"
    return "other"


def market_tag(symbol: str, venue: str = "") -> str:
    """'🪙 CRYPTO · Binance' — the what-and-where prefix every trade line
    carries. Class first, because that is what shapes the trade; venue second,
    because that is what tells you who held the money."""
    icon, label, _ = ASSET_CLASS_META.get(asset_class(symbol), UNKNOWN_CLASS)
    who = venue_name(venue)
    return f"{icon} {label} {BULLET} {who}"


def class_label(symbol: str) -> str:
    return ASSET_CLASS_META.get(asset_class(symbol), UNKNOWN_CLASS)[1]


def arrow(value: float, flat_eps: float = 1e-9) -> str:
    if value > flat_eps:
        return UP
    if value < -flat_eps:
        return DOWN
    return FLAT


# ── Emoji ────────────────────────────────────────────────────────────────
E_PROFIT = "🟢"
E_LOSS = "🔴"
E_FLAT = "⚪"
E_BUY = "🟢"
E_SELL = "🔴"
E_MONEY = "💰"
E_BALANCE = "💼"
E_FEES = "⛽"
E_TARGET = "🎯"
E_STOP = "🛑"
E_ENTRY = "💵"
E_EXIT = "💰"
E_SIZE = "📦"
E_VENUE = "🏦"
E_TIME = "⏱"
E_CALENDAR = "📅"
E_RR = "⚖️"
E_BRAIN = "🧠"
E_GLOBE = "🌐"
E_STATS = "📈"
E_WARN = "🟡"
E_BOOM = "🚀"
E_SKULL = "💀"
E_TROPHY = "🏆"
E_SHIELD = "🛡"
E_BOOK = "📒"
E_CLOCK = "🕐"
E_POWER = "🟢"
E_SLEEP = "🔌"
E_CANDLE = "🕯"


def pnl_emoji(value: float) -> str:
    if value > 0:
        return E_PROFIT
    if value < 0:
        return E_LOSS
    return E_FLAT


def signed_money(value: float, decimals: int = 2) -> str:
    """Always signed. A bare '4.10' next to a bare '−0.25' is a subtraction
    the reader has to do; '+4.10' next to '−0.25' is not."""
    return f"{value:+,.{decimals}f}"


def signed_pct(value: float, decimals: int = 2) -> str:
    return f"{value:+.{decimals}f}%"


def money(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def price(value: float, symbol: str = "") -> str:
    """Price precision scales with size: 8 decimals on a 0.00000342 alt reads
    as noise, 2 on a 68,000 gold quote reads as a rounding error."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    if v >= 0.01:
        return f"{v:.5f}"
    return f"{v:.8f}"


def qty(value: float) -> str:
    return f"{float(value):,.6f}".rstrip("0").rstrip(".")


def holding_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def utc_stamp(epoch: float = None) -> str:
    dt = datetime.fromtimestamp(epoch, timezone.utc) if epoch else datetime.now(timezone.utc)
    return dt.strftime("%d %b %H:%M UTC")


def rules(char: str = "━", width: int = 24) -> str:
    return char * width


def pnl_line(label: str, value: float, decimals: int = 2, suffix: str = " USD") -> str:
    return f"{arrow(value)} <b>{label}:</b>  <code>{signed_money(value, decimals)}{suffix}</code>"


# ── Section renderers, shared by Telegram and the 24h/6h reports ─────────

def stat_row(items: list, sep: str = "  ·  ") -> str:
    """(emoji, label, value, value_is_signed) tuples on one line."""
    parts = []
    for item in items:
        emoji, label, value, signed = (list(item) + [False])[:4]
        text = signed_money(value) if signed else str(value)
        parts.append(f"{emoji} {label} <code>{text}</code>")
    return sep.join(parts)


def daily_block(daily: dict, balance: float = None, net_after_costs: bool = True) -> str:
    """The 'what did today make me' block that closes every trade message.

    `daily` comes from StateManager.get_daily_economics(). It shows realized
    PnL separately from fees and from gas, because the question is never
    just 'did I make money' — it is 'after everything, did I make money'."""
    realized = float(daily.get("realized_pnl", 0) or 0)
    fees = float(daily.get("fees_paid", 0) or 0)
    gas = float(daily.get("network_fees", 0) or 0)
    trades = int(daily.get("trades", 0) or 0)
    wins = int(daily.get("wins", 0) or 0)
    losses = int(daily.get("losses", 0) or 0)
    wr = (wins / trades * 100) if trades else 0.0
    net = realized - gas if net_after_costs else realized

    lines = [
        f"{E_CALENDAR} <b>TODAY</b>  {arrow(net)} <code>{signed_money(net)} USD</code> "
        f"<i>({trades} trades · {wins}W {losses}L · {wr:.0f}% WR)</i>",
    ]
    if fees or gas:
        cost_parts = []
        if fees:
            cost_parts.append(f"venue {signed_money(-fees)}")
        if gas:
            cost_parts.append(f"gas {signed_money(-gas)}")
        lines.append(f"{E_FEES} <b>COSTS PAID</b>  <code>{'  |  '.join(cost_parts)} USD</code>")
    if balance is not None:
        lines.append(f"{E_BALANCE} <b>BALANCE</b>  <code>{money(balance)} USD</code>")
    return "\n".join(lines)


# ── Derived quality metrics ──────────────────────────────────────────────
# These are the numbers that decide whether to keep running the bot, and they
# are defined here — once — so the digest, the 6-hour report, the dashboard
# and the control bots cannot each mean something different by "efficiency".

def accuracy_pct(wins: int, total: int) -> float:
    """Share of closed trades that made money, after costs."""
    return (wins / total * 100) if total else 0.0


def efficiency_pct(net_pnl: float, gross_pnl: float) -> float:
    """What fraction of the price move survived execution costs.

    This is the metric that catches a strategy which is right and still losing
    money. A 70%-win-rate system paying 10bps a side on 3% targets keeps ~93%
    of its gross; one paying 30bps keeps ~80%, and the difference is invisible
    in a win rate. Above 100% means fees were refunded or the gross figure is
    understated; below 60% means costs are eating the edge, not the entries.
    """
    if gross_pnl <= 0:
        return 0.0
    return net_pnl / gross_pnl * 100


def capital_efficiency_pct(net_pnl: float, balance: float) -> float:
    """Return on capital actually at risk, per the period."""
    return (net_pnl / balance * 100) if balance else 0.0


def expectancy_pct(net_pnl: float, trades: int, balance: float) -> float:
    """Average net result per trade, as a percentage of the balance at risk.
    The single number that says whether repeating this is worth it."""
    if not trades or not balance:
        return 0.0
    return (net_pnl / trades / balance) * 100


def avg_win_loss(pnls: list) -> tuple:
    """(mean win, mean loss) in currency. Both are positive magnitudes; the
    loss is what was given up, not a negative to be summed."""
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    return avg_win, abs(avg_loss)
