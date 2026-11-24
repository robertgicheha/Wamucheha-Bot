"""
Regression tests for the shared decision path (strategy/decision.py).

  * evaluate() crashed with TypeError on every bar that produced a signal,
    because it compared the (buy, sell) tuples in raw_scores with 0. Live
    trading and backtests both go through it, so neither could trade.
  * The per-bar row cache in technical_strategy must be invisible: scores with
    and without it have to be identical.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import decision, technical_strategy as ts
from strategy.backtester import run_backtest


def _df(n=400, drift=0.0006, seed=5):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(drift, 0.008, n))
    o = np.r_[100, c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.003,
                         "low": np.minimum(o, c) * 0.997, "close": c,
                         "volume": rng.uniform(100, 1000, n)},
                        index=pd.date_range("2024-01-01", periods=n, freq="15min"))


def test_evaluate_never_raises_over_many_bars():
    df = ts.compute_indicators(_df(), {})
    fired = 0
    for i in range(60, len(df), 3):
        sig = decision.evaluate(df.iloc[:i + 1], 0.02, 1000.0, cfg={},
                                _df_has_indicators=True, symbol="TEST")
        if sig:
            fired += 1
            assert sig["symbol"] == "TEST" and sig["side"] in ("buy", "sell")
    assert fired > 0, "fixture should produce at least one signal to exercise the logging path"


def test_row_cache_does_not_change_scores():
    df = ts.compute_indicators(_df(), {})
    with_cache, regime_a = ts.compute_raw_scores(df, {})
    # Force the uncached path by bypassing the cache helper.
    original = ts._row
    ts._row = lambda d, pos: d.iloc[pos]
    try:
        without, regime_b = ts.compute_raw_scores(df, {})
    finally:
        ts._row = original
    assert regime_a == regime_b
    assert with_cache == without


def test_cache_is_cleared_after_scoring():
    df = ts.compute_indicators(_df(), {})
    ts.compute_raw_scores(df, {})
    assert getattr(ts._row_cache_tls, "cache", None) is None


def test_backtest_runs_end_to_end():
    r = run_backtest(_df(300), symbol="TEST")
    assert r["num_trades"] >= 0 and np.isfinite(r["final_balance"])
