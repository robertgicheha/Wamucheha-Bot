"""
Enhanced hourly trading report — with styled summaries pushed to Telegram and Discord.

Computes comprehensive hourly stats and pushes rich-formatted reports.
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("hourly_report")
HOURLY_LOG = Path(__file__).parent.parent / "data" / "hourly_log.jsonl"


class HourlyReporter:
    def __init__(self, state_manager, notifier, interval_minutes: int = 60):
        self.state = state_manager
        self.notifier = notifier
        self.interval_minutes = interval_minutes
        self._last_report = 0
        HOURLY_LOG.parent.mkdir(parents=True, exist_ok=True)

    def maybe_report(self):
        import time
        now = time.time()
        if now - self._last_report < self.interval_minutes * 60:
            return
        self._last_report = now

        risk_state = self.state.get_risk_state()
        positions = self.state.get_open_positions()

        stats = _compute_hourly_stats(self.state)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trading_balance": risk_state["trading_balance"],
            "daily_pnl": risk_state["daily_pnl"],
            "open_positions": len(positions),
            "trades_this_hour": stats["trades_this_hour"],
            "wins_this_hour": stats["wins_this_hour"],
            "losses_this_hour": stats["losses_this_hour"],
            "hour_pnl": stats["hour_pnl"],
            "consecutive_losses": risk_state["consecutive_losses"],
            "trading_halted": bool(risk_state["trading_halted"]),
            "symbols_traded": stats.get("symbols_traded", []),
            "best_trade": stats.get("best_trade", 0),
            "worst_trade": stats.get("worst_trade", 0),
        }

        with open(HOURLY_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

        # Push styled summary to Telegram and Discord
        self.notifier.notify_hourly_summary(entry)


def _compute_hourly_stats(state_manager) -> dict:
    """Compute comprehensive hourly statistics using SQLAlchemy."""
    from sqlalchemy.orm import Session
    from core.state_manager import TradeRow, engine

    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    with Session(engine) as session:
        trades = session.query(TradeRow).filter(
            TradeRow.closed_at >= one_hour_ago,
            TradeRow.status == "closed",
        ).all()

        trade_count = len(trades)
        wins = sum(1 for t in trades if t.pnl is not None and t.pnl > 0)
        losses = sum(1 for t in trades if t.pnl is not None and t.pnl <= 0)
        hour_pnl = sum(t.pnl for t in trades if t.pnl is not None)

        symbols = list(set(t.symbol for t in trades))
        pnl_values = [t.pnl for t in trades if t.pnl is not None]
        best_trade = max(pnl_values) if pnl_values else 0
        worst_trade = min(pnl_values) if pnl_values else 0

    return {
        "trades_this_hour": trade_count,
        "wins_this_hour": wins,
        "losses_this_hour": losses,
        "hour_pnl": hour_pnl,
        "symbols_traded": symbols,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
    }


def read_hourly_log(n: int = 24) -> list:
    if not HOURLY_LOG.exists():
        return []
    lines = HOURLY_LOG.read_text().strip().splitlines()[-n:]
    return [json.loads(l) for l in reversed(lines)]
