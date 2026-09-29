"""
Two scheduled reports, and only two.

  6 hours  — "is this working right now?" Net result after costs, record,
             balance, what is open, what the bot chose not to tell you.
  24 hours — "was the bot even on, and what did the day actually cost me?"
             Uptime and restart count first, because a P&L figure printed
             next to no uptime information cannot be interpreted: a red day
             from a bot that was up is an edge problem, a red day from a bot
             that was restarting is an infrastructure problem, and they need
             opposite responses.

Why not hourly: the old hourly summary fired 24 times a day, most of them
saying "0 trades, 0.00, unchanged". That trains a reader to swipe past the
channel, and the two alerts a week that actually need a human get swiped
along with them. Every individual trade is messaged the instant it closes,
so a periodic report has no job restating them. A periodic report's only
reason to exist is to aggregate and conclude.

Why the 24-hour report runs even with no trades: "the bot has been on, it
placed nothing, and here is why that is the right outcome" is a real and
important message. Silence is indistinguishable from a crash.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("session_report")

REPORT_LOG = Path(__file__).parent.parent / "data" / "session_reports.jsonl"
# Kept for backwards compatibility: the dashboard and both control bots read
# this file through read_hourly_log(). Historical entries stay readable and
# new reports land alongside them.
LEGACY_HOURLY_LOG = Path(__file__).parent.parent / "data" / "hourly_log.jsonl"


class SessionReporter:
    def __init__(self, state_manager, notifier, risk_manager=None,
                 window_hours: int = 6, day_hours: int = 24,
                 heartbeat_path: Path = None):
        self.state = state_manager
        self.notifier = notifier
        self.risk = risk_manager
        self.window_hours = window_hours
        self.day_hours = day_hours
        self.heartbeat_path = heartbeat_path or (
            Path(__file__).parent.parent / "data" / "engine_heartbeat.json")
        REPORT_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Both timers start at construction, so the first report lands one
        # full window after the bot comes up rather than instantly at boot
        # with an empty ledger.
        self._last_window = time.time()
        self._last_day = time.time()

    # ---------- entry point, called once per main loop tick ----------

    def maybe_report(self):
        now = time.time()
        if now - self._last_window >= self.window_hours * 3600:
            self._last_window = now
            try:
                self._emit_window(now)
            except Exception as e:
                logger.error(f"window report failed: {e}")
        if now - self._last_day >= self.day_hours * 3600:
            self._last_day = now
            try:
                self._emit_day(now)
            except Exception as e:
                logger.error(f"daily report failed: {e}")

    # ---------- 6-hour ----------

    def _emit_window(self, epoch: float):
        report = self.build_window_report(epoch)
        self._append(REPORT_LOG, report)
        self._append(LEGACY_HOURLY_LOG, report)
        self.notifier.notify_window_report(report)

    def build_window_report(self, epoch: float = None) -> dict:
        hours = self.window_hours
        stats = self._trade_stats_since(hours)
        risk_state = self.state.get_risk_state()
        positions = self.state.get_open_positions()
        open_notional = sum(_notional(p) for p in positions)
        fee_model = getattr(self.notifier, "fee_model", None)

        report = {
            "kind": f"{hours}h",
            "epoch": epoch or time.time(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "pnl": stats["net_pnl"],
            "gross_pnl": stats["gross_pnl"],
            "fees": stats["fees"],
            "best_trade": stats["best"],
            "worst_trade": stats["worst"],
            "symbols": stats["symbols"],
            "trading_balance": risk_state.get("trading_balance", 0.0),
            "open_positions": len(positions),
            "open_notional": open_notional,
            "trading_halted": bool(risk_state.get("trading_halted")),
            "halt_reason": risk_state.get("halt_reason"),
            "consecutive_losses": risk_state.get("consecutive_losses", 0),
            "daily": self.state.get_daily_economics(),
            "suppressed": self.notifier.drain_suppressed(),
        }
        if fee_model is not None:
            report["network"] = fee_model.network
            report["network_spend"] = fee_model.network_spend()
        return report

    # ---------- 24-hour ----------

    def _emit_day(self, epoch: float):
        report = self.build_daily_report(epoch)
        self._append(REPORT_LOG, report)
        self.notifier.notify_daily_report(report)

    def build_daily_report(self, epoch: float = None) -> dict:
        hours = self.day_hours
        stats = self._trade_stats_since(hours)
        risk_state = self.state.get_risk_state()
        balance = risk_state.get("trading_balance", 0.0)
        fee_model = getattr(self.notifier, "fee_model", None)
        network = fee_model.network_spend() if fee_model is not None else {
            "network_fee_usdt": 0.0, "network": "", "transfers": 0}

        uptime, restarts, health = self._uptime()

        report = {
            "kind": f"{hours}h",
            "epoch": epoch or time.time(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "uptime_seconds": uptime,
            "restarts": restarts,
            "health": health,
            "halt_reason": risk_state.get("halt_reason"),
            "trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "pnl": stats["net_pnl"],
            "gross_pnl": stats["gross_pnl"],
            "fees": stats["fees"],
            "network_fees": network["network_fee_usdt"],
            "network": network["network"],
            "start_balance": self.notifier.get_session_stats().get("start_balance", balance),
            "trading_balance": balance,
            "open_positions": len(self.state.get_open_positions()),
            "all_time": self.state.get_all_time_stats(),
            "week": self.state.get_daily_economics_range(7),
            "fee_totals": self.state.get_fee_totals(),
        }
        # Only send the 24h uptime report when the run is genuinely complete.
        # A process that has been up 4 minutes should not be reporting on
        # "24 hours" — a report that lies about its own window is worse than
        # no report, and a restart loop would otherwise emit a full 24-hour
        # report every few minutes.
        if uptime < self.day_hours * 3600 * 0.9 and restarts > 0:
            report["health"] = "restarting"
        return report

    # ---------- helpers ----------

    def _trade_stats_since(self, hours: int) -> dict:
        from sqlalchemy import func
        from sqlalchemy.orm import Session
        from core.state_manager import TradeRow, engine

        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        with Session(engine) as session:
            rows = session.query(TradeRow).filter(
                TradeRow.status == "closed",
                TradeRow.closed_at.isnot(None),
            ).all()

        selected = []
        for r in rows:
            closed = _parse_iso(r.closed_at)
            if closed is not None and closed.timestamp() >= cutoff:
                selected.append(r)

        pnl_values = [(r.pnl or 0.0) for r in selected]
        fees = [(r.fees or 0.0) for r in selected]
        return {
            "trades": len(selected),
            "wins": sum(1 for v in pnl_values if v > 0),
            "losses": sum(1 for v in pnl_values if v <= 0),
            "net_pnl": round(sum(pnl_values), 4),
            "gross_pnl": round(sum(pnl_values) + sum(fees), 4),
            "fees": round(sum(fees), 4),
            "best": round(max(pnl_values), 4) if pnl_values else 0.0,
            "worst": round(min(pnl_values), 4) if pnl_values else 0.0,
            "symbols": sorted({r.symbol for r in selected}),
        }

    def _uptime(self):
        """Uptime and restart count, read from a heartbeat file the engine
        rewrites on every tick. Survives a report that fires before the first
        heartbeat of a fresh process by falling back to 'starting'."""
        try:
            if self.heartbeat_path.exists():
                data = json.loads(self.heartbeat_path.read_text())
                uptime = float(data.get("uptime_seconds", 0.0))
                restarts = int(data.get("restarts", 0))
                health = data.get("health", "running")
                return uptime, restarts, health
        except Exception as e:
            logger.warning(f"heartbeat unreadable: {e}")
        return 0.0, 0, "starting"

    @staticmethod
    def _append(path: Path, entry: dict):
        try:
            with open(path, "a") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.warning(f"could not write {path.name}: {e}")


def _notional(pos: dict) -> float:
    try:
        return float(pos["amount"]) * float(pos["entry_price"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def read_hourly_log(n: int = 24) -> list:
    """Newest first. Reads both files so the dashboard's history endpoint
    keeps working across the upgrade."""
    out = []
    for path in (REPORT_LOG, LEGACY_HOURLY_LOG):
        if not path.exists():
            continue
        for line in path.read_text().strip().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    out.sort(key=lambda e: e.get("epoch") or 0, reverse=True)
    return out[:n]
