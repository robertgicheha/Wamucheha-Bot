"""
Hourly trading report — summaries of trades, wins/losses, PnL, and balance.

Writes an hourly log entry to data/hourly_log.jsonl and optionally pushes
a summary to Telegram/Discord. The dashboard reads this log for the
/hourly-logs endpoint.
"""
import json
import logging
from datetime import datetime, timezone
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
        }

        with open(HOURLY_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

        summary = (
            f"Hourly Report: {stats['trades_this_hour']} trades "
            f"({stats['wins_this_hour']}W/{stats['losses_this_hour']}L), "
            f"PnL: {stats['hour_pnl']:+.2f} USD, "
            f"Balance: {risk_state['trading_balance']:.2f} USD, "
            f"Open: {len(positions)}"
        )
        self.notifier.notify("hourly_report", summary)


def _compute_hourly_stats(state_manager) -> dict:
    conn = state_manager.conn
    from datetime import timedelta
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    cur = conn.execute(
        "SELECT status, pnl FROM trades WHERE closed_at >= ?",
        (one_hour_ago,),
    )
    rows = cur.fetchall()
    trades = len(rows)
    wins = sum(1 for r in rows if r[1] is not None and r[1] > 0)
    losses = sum(1 for r in rows if r[1] is not None and r[1] <= 0)
    hour_pnl = sum(r[1] for r in rows if r[1] is not None)

    return {
        "trades_this_hour": trades,
        "wins_this_hour": wins,
        "losses_this_hour": losses,
        "hour_pnl": hour_pnl,
    }


def read_hourly_log(n: int = 24) -> list:
    if not HOURLY_LOG.exists():
        return []
    lines = HOURLY_LOG.read_text().strip().splitlines()[-n:]
    return [json.loads(l) for l in reversed(lines)]
