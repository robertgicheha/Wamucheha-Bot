import numpy as np
import pandas as pd
import pytest

from strategy.fx_trend import (
    TrendParams, compute_indicators, currency_legs, initial_stop,
    position_notional, trail_stop,
)
from strategy.fx_trend_backtest import BtConfig, monte_carlo, run_backtest


def make_df(closes, spread=0.002):
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="D")
    c = np.asarray(closes, float)
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + spread / 2),
                         "low": np.minimum(o, c) * (1 - spread / 2), "close": c}, index=idx)


def trending(n=400, drift=0.0015, seed=1):
    rng = np.random.default_rng(seed)
    return 1.1 * np.cumprod(1 + drift + rng.normal(0, 0.003, n))


def test_signal_has_no_lookahead():
    closes = trending(300)
    full = compute_indicators(make_df(closes))
    part = compute_indicators(make_df(closes[:200]))
    assert (full["signal"].iloc[:200].values == part["signal"].values).all()


def test_warmup_is_flat_and_uptrend_is_long():
    ind = compute_indicators(make_df(trending(300)))
    assert (ind["signal"].iloc[:90] == 0).all()
    assert ind["signal"].iloc[-1] == 1


def test_downtrend_short_only_when_allowed():
    closes = trending(300, drift=-0.0015)
    assert compute_indicators(make_df(closes))["signal"].iloc[-1] == -1
    p = TrendParams(allow_short=False)
    assert compute_indicators(make_df(closes), p)["signal"].iloc[-1] == 0


def test_trail_stop_never_loosens():
    assert trail_stop(1, 1.10, 1.12, 1.11, 0.005, 3) == pytest.approx(1.105)
    assert trail_stop(1, 1.10, 1.101, 1.09, 0.005, 3) == 1.10        # would loosen -> kept
    assert trail_stop(-1, 1.20, 1.19, 1.18, 0.005, 3) == pytest.approx(1.195)


def test_sizing_loses_risk_pct_at_stop():
    entry, stop = 1.10, initial_stop(1, 1.10, 0.005, 3.0)
    n = position_notional(10_000, 0.5, entry, stop, max_leverage=50)
    assert n * (entry - stop) / entry == pytest.approx(50.0)          # 0.5% of 10k
    assert position_notional(10_000, 0.5, entry, entry - 1e-9, 5) == 10_000 * 5  # leverage cap


def test_currency_legs():
    assert currency_legs("EURUSD", 1) == {"EUR": 1, "USD": -1}
    assert currency_legs("EUR/USD", -1) == {"EUR": -1, "USD": 1}


def test_backtest_profits_in_clean_trend_and_costs_hurt():
    frames = {"EURUSD": make_df(trending(500, drift=0.002))}
    free = run_backtest(frames, cfg=BtConfig(spread_pips={"EURUSD": 0.0}, slippage_pips=0, swap_bps_per_day=0))
    costly = run_backtest(frames, cfg=BtConfig(spread_pips={"EURUSD": 5.0}, slippage_pips=2, swap_bps_per_day=3))
    assert free["stats"]["total_return_pct"] > 0
    assert costly["stats"]["total_return_pct"] < free["stats"]["total_return_pct"]


def test_currency_exposure_cap_blocks_extra_usd_bets():
    frames = {p: make_df(trending(400, drift=0.002, seed=i)) for i, p in
              enumerate(["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"], 1)}
    capped = run_backtest(frames, cfg=BtConfig(max_net_per_currency=2))
    open_at_once = max(
        sum(1 for t in capped["trades"] if t.entry_time <= d <= t.exit_time)
        for d in capped["equity"].index)
    assert open_at_once <= 2


def test_monte_carlo_needs_enough_trades():
    assert "note" in monte_carlo([])
