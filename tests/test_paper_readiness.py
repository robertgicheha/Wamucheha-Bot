"""The readiness gate must fail closed: no data, thin data, or a lucky streak is NOT ready."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.paper_readiness import evaluate_readiness  # noqa: E402

NOW = datetime(2025, 3, 1, tzinfo=timezone.utc)


def _trades(pnls, days=30):
    out = []
    for i, p in enumerate(pnls):
        t = NOW - timedelta(days=days * (1 - i / max(len(pnls) - 1, 1)))
        out.append({"pnl": p, "fees": 0.01, "opened_at": t.isoformat(),
                    "closed_at": (t + timedelta(hours=2)).isoformat(), "status": "closed"})
    return out


def _run(trades, **kw):
    args = dict(balance=1000.0, max_drawdown_limit_pct=5.0, max_position_pct=3.0,
                restarts=0, uncaught_errors_7d=0, now=NOW)
    args.update(kw)
    return evaluate_readiness(trades, **args)


def _ready(res):
    return all(r["ok"] for r in res if r["required"])


def test_no_trades_is_not_ready():
    assert not _ready(_run([]))


def test_too_few_trades_is_not_ready():
    assert not _ready(_run(_trades([2.0, -1.0] * 5)))


def test_too_short_a_period_is_not_ready():
    assert not _ready(_run(_trades([2.0, -1.0] * 20, days=3)))


def test_solid_record_is_ready():
    assert _ready(_run(_trades([2.0, -1.0, 2.5, -0.8] * 15)))


def test_losing_record_is_not_ready():
    res = _run(_trades([-1.0, 0.5] * 30))
    assert not _ready(res)
    assert not next(r for r in res if r["name"] == "Profitable after fees")["ok"]


def test_one_catastrophic_loss_blocks_even_if_profitable():
    res = _run(_trades([2.0, -1.0, 2.5, -0.8] * 15 + [-120.0]))
    assert not next(r for r in res if r["name"] == "No outsized single loss")["ok"]
    assert not _ready(res)


def test_crash_loops_and_uncaught_errors_block():
    good = _trades([2.0, -1.0, 2.5, -0.8] * 15)
    assert not _ready(_run(good, restarts=9))
    assert not _ready(_run(good, uncaught_errors_7d=3))


def test_edge_must_be_statistically_positive():
    # Barely positive on average with huge variance: profitable by luck, not provable.
    res = _run(_trades([10.0, -9.5] * 15))
    assert not next(r for r in res if r["name"] == "Edge statistically positive (95%)")["ok"]
