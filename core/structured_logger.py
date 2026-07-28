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
"""
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from collections import defaultdict

LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# JSON log file (rotated daily)
_log_file = None
_current_date = None


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
                   exchange: str, strategies: list = None, score: float = 0):
    _write_entry({
        "type": "trade_open",
        "symbol": symbol, "side": side, "amount": amount,
        "price": price, "exchange": exchange,
        "strategies": strategies or [], "score": score,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def log_trade_close(symbol: str, side: str, entry_price: float, exit_price: float,
                    pnl: float, reason: str, exchange: str):
    _write_entry({
        "type": "trade_close",
        "symbol": symbol, "side": side,
        "entry_price": entry_price, "exit_price": exit_price,
        "pnl": pnl, "reason": reason, "exchange": exchange,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def log_signal(symbol: str, action: str, confidence: float,
               conflicts: list, regime: str, strategies: list):
    _write_entry({
        "type": "signal",
        "symbol": symbol, "action": action, "confidence": confidence,
        "conflicts": conflicts, "regime": regime, "strategies": strategies,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def log_risk_event(event_type: str, details: str, priority: str = "normal"):
    _write_entry({
        "type": "risk_event",
        "event": event_type, "details": details, "priority": priority,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def log_system_event(event_type: str, details: str):
    _write_entry({
        "type": "system",
        "event": event_type, "details": details,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
