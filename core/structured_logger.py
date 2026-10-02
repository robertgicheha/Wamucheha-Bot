"""
Structured Logger — JSON logging, slippage tracking, API failure monitoring.

Replaces ad-hoc print() calls with structured JSON log entries that can be:
  - Parsed by log aggregators (ELK, Grafana Loki, etc.)
  - Queried for slippage analysis, API health, strategy performance
  - Stored in data/logs/ for offline analysis

Features:
  - Structured JSON log entries with consistent schema
  - Slippage tracking: signal price vs actual fill price
  - API failure rate monitoring per exchange
  - Strategy performance tracking
  - Circuit breaker event logging
  - Verbose console output for live trading monitoring
"""
import json
import time
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from collections import defaultdict

LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# JSON log file (rotated daily)
_log_file = None
_current_date = None

# Verbose mode for live trading - controlled by VERBOSE_LOGGING env var
VERBOSE_LOGGING = os.environ.get("VERBOSE_LOGGING", "false").lower() == "true"


def _get_log_file() -> Path:
    global _log_file, _current_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _current_date != today:
        _current_date = today
        _log_file = LOG_DIR / f"bot_{today}.jsonl"
    return _log_file


def _write_entry(entry: dict):
    """Write a structured JSON log entry."""
    try:
        with open(_get_log_file(), "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _verbose_print(msg: str, level: str = "INFO"):
    """Print to console if verbose logging is enabled."""
    if VERBOSE_LOGGING:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        prefix = {"INFO": "[INFO]", "TRADE": "[TRADE]", "FEE": "[FEE]", "RISK": "[RISK]", "SLIP": "[SLIP]", "SIGNAL": "[SIGNAL]", "SYS": "[SYS]"}.get(level, "[INFO]")
        print(f"[{timestamp}] {prefix} {msg}")


# ---------- Slippage Tracker ----------

@dataclass
class SlippageRecord:
    symbol: str
    side: str
    signal_price: float
    fill_price: float
    timestamp: str
    exchange: str = ""
    slippage_pct: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self):
        if self.signal_price > 0:
            self.slippage_pct = (self.fill_price - self.signal_price) / self.signal_price * 100
            if self.side == "sell":
                self.slippage_pct = -self.slippage_pct
            self.slippage_bps = self.slippage_pct * 100


class SlippageTracker:
    """Track and analyze slippage across all trades."""

    def __init__(self, window_size: int = 100):
        self.records: list[SlippageRecord] = []
        self.window_size = window_size

    def record(self, symbol: str, side: str, signal_price: float,
               fill_price: float, exchange: str = ""):
        """Record a trade's slippage."""
        rec = SlippageRecord(
            symbol=symbol,
            side=side,
            signal_price=signal_price,
            fill_price=fill_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            exchange=exchange,
        )
        self.records.append(rec)

        # Keep only recent window
        if len(self.records) > self.window_size:
            self.records = self.records[-self.window_size:]

        # Log it
        _write_entry({
            "type": "slippage",
            "symbol": symbol,
            "side": side,
            "signal_price": signal_price,
            "fill_price": fill_price,
            "slippage_pct": round(rec.slippage_pct, 4),
            "slippage_bps": round(rec.slippage_bps, 2),
            "exchange": exchange,
            "ts": rec.timestamp,
        })

        # Alert if slippage exceeds threshold
        if abs(rec.slippage_bps) > 50:  # > 0.5%
            from alerts.notifier import _global_notifier
            if _global_notifier:
                _global_notifier.notify("high_slippage",
                    f"HIGH SLIPPAGE: {symbol} {side} "
                    f"signal={signal_price:.4f} fill={fill_price:.4f} "
                    f"({rec.slippage_bps:.1f} bps)",
                    priority="normal")

    def get_stats(self) -> dict:
        """Slippage statistics over recent window."""
        if not self.records:
            return {"count": 0}

        slippages = [r.slippage_bps for r in self.records]
        by_exchange = defaultdict(list)
        by_symbol = defaultdict(list)
        for r in self.records:
            by_exchange[r.exchange].append(r.slippage_bps)
            by_symbol[r.symbol].append(r.slippage_bps)

        return {
            "count": len(self.records),
            "avg_bps": round(sum(slippages) / len(slippages), 2),
            "max_bps": round(max(slippages), 2),
            "min_bps": round(min(slippages), 2),
            "by_exchange": {
                ex: round(sum(v) / len(v), 2) for ex, v in by_exchange.items()
            },
            "by_symbol": {
                s: round(sum(v) / len(v), 2) for s, v in by_symbol.items()
            },
        }


# ---------- API Failure Tracker ----------

class APIFailureTracker:
    """Track API failures per exchange for health monitoring."""

    def __init__(self, alert_threshold: int = 5):
        self.failures: dict[str, list[dict]] = defaultdict(list)
        self.alert_threshold = alert_threshold
        self.window_seconds = 300  # 5-minute rolling window

    def record_failure(self, exchange: str, error: str, endpoint: str = ""):
        """Record an API failure."""
        now = time.time()
        entry = {"ts": now, "error": str(error)[:200], "endpoint": endpoint}
        self.failures[exchange].append(entry)

        # Prune old entries
        cutoff = now - self.window_seconds
        self.failures[exchange] = [
            e for e in self.failures[exchange] if e["ts"] > cutoff
        ]

        # Log it
        _write_entry({
            "type": "api_failure",
            "exchange": exchange,
            "error": str(error)[:200],
            "endpoint": endpoint,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # Alert if too many failures
        recent_count = len(self.failures[exchange])
        if recent_count >= self.alert_threshold:
            from alerts.notifier import _global_notifier
            if _global_notifier:
                _global_notifier.notify("api_failure_burst",
                    f"API FAILURE BURST: {exchange} has {recent_count} failures "
                    f"in last {self.window_seconds}s. Last: {str(error)[:100]}",
                    priority="high")

    def record_success(self, exchange: str):
        """Record a successful API call (resets failure context)."""
        pass  # failures are pruned by time window

    def get_health(self) -> dict:
        """API health status per exchange."""
        now = time.time()
        cutoff = now - self.window_seconds
        result = {}
        for ex, entries in self.failures.items():
            recent = [e for e in entries if e["ts"] > cutoff]
            result[ex] = {
                "failures_last_5m": len(recent),
                "healthy": len(recent) < self.alert_threshold,
                "last_error": recent[-1]["error"] if recent else None,
            }
        return result


# ---------- Strategy Performance Tracker ----------

class StrategyPerformanceTracker:
    """Track per-strategy win rate and PnL for adaptive muting."""

    def __init__(self, lookback: int = 50):
        self.lookback = lookback
        self.trades: dict[str, list[dict]] = defaultdict(list)

    def record_trade(self, strategies: list[str], pnl: float, symbol: str):
        """Record a closed trade's outcome per strategy."""
        for strat in strategies:
            self.trades[strat].append({
                "pnl": pnl,
                "symbol": symbol,
                "ts": time.time(),
            })
            # Prune old
            if len(self.trades[strat]) > self.lookback:
                self.trades[strat] = self.trades[strat][-self.lookback:]

        _write_entry({
            "type": "strategy_trade",
            "strategies": strategies,
            "pnl": pnl,
            "symbol": symbol,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def get_strategy_stats(self) -> dict:
        """Per-strategy performance summary."""
        result = {}
        for strat, trades in self.trades.items():
            if not trades:
                continue
            wins = sum(1 for t in trades if t["pnl"] > 0)
            total_pnl = sum(t["pnl"] for t in trades)
            result[strat] = {
                "trades": len(trades),
                "wins": wins,
                "win_rate": round(wins / len(trades) * 100, 1),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(total_pnl / len(trades), 2),
            }
        return result

    def get_underperforming(self, min_trades: int = 10,
                            min_win_rate: float = 35.0) -> list[str]:
        """Return strategies that should be considered for muting."""
        underperforming = []
        for strat, trades in self.trades.items():
            if len(trades) < min_trades:
                continue
            wins = sum(1 for t in trades if t["pnl"] > 0)
            win_rate = wins / len(trades) * 100
            if win_rate < min_win_rate:
                underperforming.append(strat)
        return underperforming


# ---------- Global instances ----------

slippage_tracker = SlippageTracker()
api_failure_tracker = APIFailureTracker()
strategy_perf_tracker = StrategyPerformanceTracker()


# ---------- Structured log helpers ----------

def log_trade_open(symbol: str, side: str, amount: float, price: float,
                   exchange: str, strategies: list = None, score: float = 0,
                   entry_fee: float = 0.0, order_id: str = "", stop_loss: float = 0.0,
                   take_profit: float = 0.0, risk_pct: float = 0.0, proposed_amount: float = 0.0):
    notional = round(float(price) * float(amount), 4)
    _write_entry({
        "type": "trade_open",
        "symbol": symbol, "side": side, "amount": amount,
        "price": price, "exchange": exchange,
        "strategies": strategies or [], "score": score,
        "entry_fee": entry_fee,
        "notional": notional,
        "order_id": order_id,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_pct": risk_pct,
        "proposed_amount": proposed_amount,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    
    _verbose_print(
        f"TRADE OPENED | {symbol} {side.upper()} | "
        f"Size: {amount:.6f} @ {price:.4f} | Notional: ${notional:.2f} | "
        f"Exchange: {exchange} | Fee: ${entry_fee:.4f} | "
        f"SL: {stop_loss:.4f} | TP: {take_profit:.4f} | "
        f"Strategies: {', '.join(strategies or [])} | Score: {score:.3f} | "
        f"OrderID: {order_id}",
        "TRADE"
    )


def log_trade_close(symbol: str, side: str, entry_price: float, exit_price: float,
                    pnl: float, reason: str, exchange: str,
                    gross_pnl: float = None, fees: float = 0.0,
                    entry_fee: float = 0.0, exit_fee: float = 0.0,
                    order_id: str = "", held_seconds: float = 0.0, amount: float = 0.0):
    # gross and net are both recorded. A log that only holds the net number
    # cannot answer 'was this trade a good idea that fees ate, or a bad idea
    # outright' — and that is the question you need when the day is red.
    _write_entry({
        "type": "trade_close",
        "symbol": symbol, "side": side,
        "entry_price": entry_price, "exit_price": exit_price,
        "pnl": pnl, "reason": reason, "exchange": exchange,
        "gross_pnl": gross_pnl if gross_pnl is not None else pnl,
        "fees": fees,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "order_id": order_id,
        "held_seconds": held_seconds,
        "amount": amount,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if side == "buy" \
        else ((entry_price - exit_price) / entry_price * 100)
    
    _verbose_print(
        f"TRADE CLOSED | {symbol} {side.upper()} | "
        f"Entry: {entry_price:.4f} -> Exit: {exit_price:.4f} | "
        f"PnL: ${pnl:.4f} ({pnl_pct:+.2f}%) | Gross: ${gross_pnl:.4f} | "
        f"Fees: ${fees:.4f} (Entry: ${entry_fee:.4f} + Exit: ${exit_fee:.4f}) | "
        f"Reason: {reason} | Held: {held_seconds/60:.1f}min | "
        f"Exchange: {exchange} | OrderID: {order_id}",
        "TRADE"
    )


def log_fee_event(kind: str, venue: str, amount_usd: float, detail: str = ""):
    _write_entry({
        "type": "fee",
        "kind": kind, "venue": venue, "amount_usd": amount_usd,
        "detail": detail,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _verbose_print(
        f"FEE EVENT | {kind.upper()} | Venue: {venue} | Amount: ${amount_usd:.4f} | {detail}",
        "FEE"
    )


def log_signal(symbol: str, action: str, confidence: float,
               conflicts: list, regime: str, strategies: list,
               score: float = 0.0, min_score: float = 0.0, passed: bool = True):
    _write_entry({
        "type": "signal",
        "symbol": symbol, "action": action, "confidence": confidence,
        "conflicts": conflicts, "regime": regime, "strategies": strategies,
        "score": score, "min_score": min_score, "passed": passed,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    if passed or VERBOSE_LOGGING:
        status = "[PASSED]" if passed else "[BLOCKED]"
        _verbose_print(
            f"SIGNAL {status} | {symbol} {action.upper()} | "
            f"Score: {score:.3f} (min: {min_score:.3f}) | Confidence: {confidence:.3f} | "
            f"Regime: {regime} | Strategies: {', '.join(strategies)} | "
            f"Conflicts: {', '.join(conflicts) if conflicts else 'none'}",
            "SIGNAL"
        )


def log_risk_event(event_type: str, details: str, priority: str = "normal",
                   trading_balance: float = 0.0, daily_pnl: float = 0.0,
                   consecutive_losses: int = 0, proposed_action: str = ""):
    _write_entry({
        "type": "risk_event",
        "event": event_type, "details": details, "priority": priority,
        "trading_balance": trading_balance,
        "daily_pnl": daily_pnl,
        "consecutive_losses": consecutive_losses,
        "proposed_action": proposed_action,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _verbose_print(
        f"RISK EVENT | {event_type.upper()} | Priority: {priority} | "
        f"Balance: ${trading_balance:.2f} | Daily PnL: ${daily_pnl:.2f} | "
        f"Loss Streak: {consecutive_losses} | Action: {proposed_action} | {details}",
        "RISK"
    )


def log_system_event(event_type: str, details: str, extra: dict = None):
    _write_entry({
        "type": "system",
        "event": event_type, "details": details,
        "extra": extra or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _verbose_print(
        f"SYSTEM | {event_type.upper()} | {details}" + (f" | {extra}" if extra else ""),
        "SYS"
    )


# ---------- engine heartbeat ----------
# The 24-hour report has to answer "was the bot on?" and nothing in-process
# can answer that, because the process that would answer it is the thing that
# might be dead. This file is written every tick and carries the run's start
# time plus a restart counter derived from it, so an uptime figure survives
# the very crash it is meant to describe.

HEARTBEAT_PATH = LOG_DIR.parent / "engine_heartbeat.json"
HEARTBEAT_INTERVAL = 60  # seconds between writes; the file is a status
# snapshot, not a stream, and writing it on every 15s loop tick buys nothing.


class EngineHeartbeat:
    def __init__(self, path=HEARTBEAT_PATH):
        self.path = path
        self._last_write = 0.0
        self._run_started = time.time()
        # A previous run that ended less than this long ago means this is a
        # restart, not a cold boot — worth knowing, because a crash loop looks
        # exactly like a healthy day if nobody counts restarts.
        self._restarts = self._count_restarts()

    def _count_restarts(self) -> int:
        try:
            if not self.path.exists():
                return 0
            data = json.loads(self.path.read_text())
            previous_start = float(data.get("run_started_epoch", 0))
            if previous_start and previous_start < self._run_started - 60:
                return int(data.get("restarts", 0)) + 1
            return int(data.get("restarts", 0))
        except Exception:
            return 0

    def beat(self, health: str = "running", force: bool = False):
        now = time.time()
        if not force and now - self._last_write < HEARTBEAT_INTERVAL:
            return
        self._last_write = now
        payload = {
            "run_started_epoch": self._run_started,
            "run_started": datetime.fromtimestamp(
                self._run_started, timezone.utc).isoformat(),
            "uptime_seconds": now - self._run_started,
            "restarts": self._restarts,
            "health": health,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a reader must never see a half-written file
            # and report a corrupt uptime.
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.path)
        except Exception:
            pass

    @property
    def restarts(self) -> int:
        return self._restarts

    @property
    def uptime(self) -> float:
        return time.time() - self._run_started


heartbeat = EngineHeartbeat()
