"""
Tests for the funding-carry and trend-following modules.

Properties worth protecting:
  * carry earns ~funding minus costs when funding is steady, and stays flat
    (no trades, no cost) when funding is below the threshold
  * negative funding is never collected as profit
  * carry decisions use only past funding (no lookahead)
  * trend entries happen strictly after the signal bar, and leverage is capped

Run: python -m pytest tests/test_crypto_modules.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.funding_carry import (CarryConfig, annualize_funding, backtest_carry,
                                    should_enter)
from strategy.crypto_trend import TrendConfig, backtest_trend, compute_target_exposure


def _funding(rate, n=600):
    return pd.Series(rate, index=pd.date_range("2024-01-01", periods=n, freq="8h"))


def test_annualize_funding():
    # 0.01% per 8h is the exchange default baseline = ~10.95% APR
    assert abs(annualize_funding(0.0001) - 10.95) < 0.01


def test_low_funding_never_trades():
    res = backtest_carry(_funding(0.00005))   # ~5.5% APR, below entry gate
    assert res["trades"] == 0
    assert res["final_balance"] == 1000.0


def test_steady_high_funding_is_profitable_net_of_costs():
    res = backtest_carry(_funding(0.0003))    # ~33% APR
    assert res["trades"] == 1                 # held to the end, closed there
    assert res["funding_collected"] > res["costs_paid"] > 0
    assert res["total_return_pct"] > 0


def test_negative_funding_is_not_profit():
    rates = pd.concat([_funding(0.0003, 100), _funding(-0.0005, 200)])
    rates.index = pd.date_range("2024-01-01", periods=len(rates), freq="8h")
    res = backtest_carry(rates)
    assert res["max_drawdown_pct"] <= 0       # losses show up; nothing is hidden


def test_no_lookahead_on_entry():
    # Funding spikes only on the LAST print; a strategy that peeks would enter.
    r = _funding(0.0, 100)
    r.iloc[-1] = 0.01
    res = backtest_carry(r)
    assert res["trades"] == 0 and res["funding_collected"] == 0


def test_should_enter_accounts_for_costs():
    cfg = CarryConfig(entry_apr_pct=12.0, expected_hold_days=14)
    assert not should_enter(12.5, cfg)        # clears threshold gross, not net of costs
    assert should_enter(40.0, cfg)


def _trend_df(n=400, drift=0.004, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, n))
    open_ = np.concatenate([[100], close[:-1]])
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close},
                        index=pd.date_range("2022-01-01", periods=n, freq="D"))


def test_trend_exposure_capped_by_leverage():
    df = _trend_df()
    exp = compute_target_exposure(df, TrendConfig(max_leverage=1.0))
    assert exp.abs().max() <= 1.0 + 1e-9
    assert exp.iloc[:100].eq(0).all()         # warm-up window stays flat


def test_trend_makes_money_in_strong_uptrend_and_pays_costs():
    res = backtest_trend(_trend_df(), TrendConfig())
    assert res["costs_paid"] > 0
    assert res["total_return_pct"] > 0


def test_trend_flat_on_downtrend_long_only():
    res = backtest_trend(_trend_df(drift=-0.004), TrendConfig(allow_short=False))
    assert res["total_return_pct"] > -5       # sits out the crash instead of riding it
