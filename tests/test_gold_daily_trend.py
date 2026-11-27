"""The gold daily bot: breakout rules match the research module, and the loss-streak halt."""
from pathlib import Path

import pandas as pd
import yaml

from strategy.crypto_trend import TrendConfig, compute_target_exposure
from strategy.fx_trend import TrendParams, compute_indicators
from strategy.fx_trend_backtest import BtConfig, run_backtest
from tests.test_fx_trend_runner import NOW, FakeBroker, bars, runner

ROOT = Path(__file__).resolve().parent.parent
BREAKOUT = TrendParams(rules="breakout", atr_len=20, allow_short=False)


def _loss(ticket, day, profit=-10.0):
    return {"ticket": ticket, "pair": "XAUUSD", "side": 1, "volume": 0.01,
            "open_time": f"2026-01-{day:02d}T00:00:00", "close_time": f"2026-01-{day:02d}T12:00:00",
            "entry": 1.0, "exit": 0.9, "profit": profit, "commission": 0.0, "swap": 0.0}


def test_breakout_signal_is_long_or_flat_and_matches_research_rules():
    df = pd.concat([bars(250, 0.002, seed=3, end="2025-06-30"),
                    bars(150, -0.004, seed=4, start=1.7, end="2025-11-27")])
    sig = compute_indicators(df, BREAKOUT)["signal"]
    assert set(sig) == {0, 1}
    ref = (compute_target_exposure(df, TrendConfig(atr_days=20)) > 0).astype(int)
    assert (sig.iloc[101:] == ref.iloc[101:]).all()


def test_shipped_config_is_gold_breakout_and_defaults_to_dry_run():
    y = yaml.safe_load((ROOT / "config" / "fx_trend.yaml").read_text())["fx_trend"]
    assert y["dry_run"] is True and y["pairs"] == ["XAUUSD"] and y["magic"] != 774201
    p = TrendParams(**y["strategy"])
    assert p.rules == "breakout" and not p.allow_short and p.risk_pct <= 1.0
    assert y["risk"]["max_consecutive_losses"] == 8


def test_eight_losses_in_a_row_halt_entries(tmp_path):
    b = FakeBroker({"XAUUSD": bars()})
    b.history = [_loss(i, i) for i in range(1, 9)]
    runner(tmp_path, b, params=BREAKOUT, max_consecutive_losses=8).run_once(NOW)
    assert not b.opened


def test_a_win_or_a_reset_clears_the_streak(tmp_path):
    b = FakeBroker({"XAUUSD": bars()})
    b.history = [_loss(i, i) for i in range(1, 9)] + [_loss(9, 20, profit=5.0)]
    runner(tmp_path / "win", b, params=BREAKOUT, max_consecutive_losses=8).run_once(NOW)
    assert len(b.opened) == 1
    b2 = FakeBroker({"XAUUSD": bars()})
    b2.history = [_loss(i, i) for i in range(1, 9)]
    runner(tmp_path / "reset", b2, params=BREAKOUT, max_consecutive_losses=8,
           loss_streak_reset="2026-02-01").run_once(NOW)
    assert len(b2.opened) == 1


def test_backtest_skips_trades_below_the_broker_minimum():
    frames = {"XAUUSD": bars()}
    assert run_backtest(frames, BREAKOUT)["trades"]
    assert not run_backtest(frames, BREAKOUT, BtConfig(min_units={"XAUUSD": 1e12}))["trades"]
