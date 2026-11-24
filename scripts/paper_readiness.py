"""
Paper-trading readiness gate: has the bot EARNED the right to touch real money?

scripts/preflight.py answers "is the setup safe?" (keys, venues, funding).
This answers the other question: "do the paper-trading RESULTS justify going
live?" It reads closed trades from the state DB, the engine heartbeat and the
error log, and checks each criterion below. Read-only. Exit code 0 only if every
REQUIRED check passes.

    python scripts/paper_readiness.py
    python scripts/paper_readiness.py --min-trades 50 --min-days 21

A pass is not a promise of profit. It means the bot was not obviously broken or
unprofitable over a meaningful sample, which is the minimum bar for risking
money. A fail tells you exactly what to keep watching.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reporting import analytics  # noqa: E402


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def evaluate_readiness(trades: list, balance: float, max_drawdown_limit_pct: float,
                       max_position_pct: float, restarts: int | None,
                       uncaught_errors_7d: int, min_trades: int = 30, min_days: int = 14,
                       min_profit_factor: float = 1.3, max_restarts: int = 2,
                       now: datetime | None = None) -> list:
    """Return [{name, required, ok, detail}] for each readiness criterion."""
    now = now or datetime.now(timezone.utc)
    closed = [t for t in trades if t.get("pnl") is not None]
    m = analytics.compute_metrics(closed, balance)
    proj = analytics.project_equity(closed, balance, max_drawdown_limit_pct=max_drawdown_limit_pct)
    n = m.get("trades", 0)

    stamps = [d for d in (_parse(t.get("opened_at")) for t in closed) if d]
    span_days = (max(stamps) - min(stamps)).days if len(stamps) > 1 else 0
    results = []

    def check(name, ok, detail, required=True):
        results.append({"name": name, "required": required, "ok": bool(ok), "detail": detail})

    check("Enough trades", n >= min_trades, f"{n} closed trades (need {min_trades})")
    check("Long enough", span_days >= min_days,
          f"{span_days} days of trading (need {min_days}); covers different market conditions")
    if n == 0:
        return results

    check("Profitable after fees", m["total_pnl"] > 0, f"net P&L {m['total_pnl']:+.4f}")
    pf = m.get("profit_factor")
    check("Profit factor", pf is not None and pf >= min_profit_factor,
          f"{pf} (need >= {min_profit_factor})")
    check("Drawdown within limit", m["max_drawdown_pct"] > -abs(max_drawdown_limit_pct),
          f"max drawdown {m['max_drawdown_pct']}% (halt limit {max_drawdown_limit_pct}%)")
    worst_allowed = -(2 * max_position_pct / 100.0) * balance
    check("No outsized single loss", m["worst_trade"] >= worst_allowed,
          f"worst trade {m['worst_trade']} (limit {worst_allowed:.4f} = 2x max position risk); "
          "a bigger loss means a stop failed or slippage is out of control")
    gross_profit = sum(t["pnl"] for t in closed if t["pnl"] > 0) + m["total_fees"]
    fee_share = (m["total_fees"] / gross_profit * 100) if gross_profit > 0 else 100.0
    check("Fees not eating the edge", fee_share <= 30, f"fees are {fee_share:.0f}% of gross profit (max 30%)")
    check("Edge statistically positive (95%)", proj["edge_is_positive_with_95pct_confidence"],
          f"expectancy 95% range {proj.get('expectancy_ci95')}; if this fails the profit may be luck")
    check("Engine stability", restarts is not None and restarts <= max_restarts,
          f"{restarts} restarts (max {max_restarts})" if restarts is not None else "no heartbeat found",
          required=restarts is not None)
    check("No uncaught crashes (7d)", uncaught_errors_7d == 0,
          f"{uncaught_errors_7d} uncaught exceptions in errors.log")
    return results


def _load_trades():
    from core.state_manager import StateManager
    return [t for t in StateManager(stake_amount=0).get_recent_trades(100000) if t.get("status") == "closed"]


def _uncaught_7d(log: Path) -> int:
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        return sum(1 for l in log.read_text(errors="replace").splitlines()
                   if "UNCAUGHT" in l and l[:32] >= cutoff)
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=14)
    args = ap.parse_args()

    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    from core.state_manager import SNAPSHOT_PATH
    snap = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else {}
    hb_path = ROOT / "data" / "engine_heartbeat.json"
    restarts = json.loads(hb_path.read_text()).get("restarts") if hb_path.exists() else None

    res = evaluate_readiness(
        _load_trades(), snap.get("trading_balance") or cfg["account"].get("trading_balance", 0),
        cfg["risk"].get("max_drawdown_pct", 5), cfg["risk"].get("max_position_pct", 3),
        restarts, _uncaught_7d(ROOT / "data" / "logs" / "errors.log"),
        min_trades=args.min_trades, min_days=args.min_days)

    print("\nPAPER-TRADING READINESS\n" + "=" * 60)
    for r in res:
        mark = "PASS" if r["ok"] else ("FAIL" if r["required"] else "WARN")
        print(f"[{mark}] {r['name']}: {r['detail']}")
    failed = [r for r in res if r["required"] and not r["ok"]]
    print("=" * 60)
    if failed:
        print(f"NOT READY: {len(failed)} required check(s) failed. Keep paper trading.")
        return 1
    print("READY: all required checks pass. Next: python scripts/preflight.py, then go live SMALL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
