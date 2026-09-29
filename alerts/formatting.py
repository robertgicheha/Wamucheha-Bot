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
