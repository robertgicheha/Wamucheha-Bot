"""
Self-retraining of the daily trend bot.

Properties worth protecting:
  * it never changes risk, shorting or sizing, and can only ever scale risk DOWN
  * a challenger is promoted only if every gate passes, and not too soon after the last one
  * promoted settings never reach a live account unless explicitly allowed
  * an edited yaml, or a tampered state file, wins over / is ignored by promoted settings
  * the rules are never swapped under an open position
  * indicators for the new ensemble rules do not look ahead
"""
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
import yaml  # noqa: E402

from core import auto_train as at  # noqa: E402
from core.fx_trend_runner import FxTrendRunner, RunnerConfig  # noqa: E402
from strategy.fx_trend import TrendParams, compute_indicators  # noqa: E402

BASE = TrendParams(rules="breakout", atr_len=20, allow_short=False, risk_pct=0.75)
CFG = at.settings(None)


def frame(n=3300, seed=1, drift=0.0004):
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    index = pd.bdate_range("2014-01-01", periods=n)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.004,
                         "low": np.minimum(open_, close) * 0.996, "close": close}, index=index)


def promoted(params=None, base=BASE):
    params = params or replace(BASE, rules="ensemble", sma_len=200)
    return {"params": at.tuned_values(params), "config_params": at.tuned_values(base),
            "promoted_at": "2026-01-01T00:00:00+00:00"}


def test_search_space_never_touches_risk_or_direction():
    grid = at.candidates(BASE)
    assert len(grid) == 63 and len({tuple(at.tuned_values(p).values()) for p in grid}) == 63
    assert {(p.risk_pct, p.allow_short, p.max_leverage_per_pos, p.atr_len) for p in grid} == {(0.75, False, 5.0, 20)}
    assert not {"risk_pct", "allow_short", "max_leverage_per_pos"} & set(at.TUNABLE)
    assert at.settings({"degraded_risk_scale": 3})["degraded_risk_scale"] == 1.0


def test_ensemble_signal_is_long_only_and_does_not_look_ahead():
    df = frame(600)
    p = replace(BASE, rules="ensemble", sma_len=100)
    full = compute_indicators(df, p)["signal"]
    assert set(full.unique()) <= {0, 1} and full.any()
    pd.testing.assert_series_equal(compute_indicators(df.iloc[:450], p)["signal"], full.iloc[:450])


def test_costs_are_given_in_price_units():
    bt = at.cost_config(["XAUUSD"], CFG)
    assert bt.spread_pips["XAUUSD"] * 0.0001 == pytest.approx(0.50)      # not the near-zero pip default
    assert bt.slippage_pips == 0
    with pytest.raises(ValueError, match="costs"):
        at.cost_config(["EURUSD"], CFG)


def test_evaluate_needs_enough_history_and_reports_every_gate():
    with pytest.raises(ValueError, match="years"):
        at.evaluate({"XAUUSD": frame(900)}, BASE, CFG)
    report = at.evaluate({"XAUUSD": frame()}, BASE, CFG)
    assert len(report["checks"]) == 9 and report["candidates"] == 63
    assert report["passed"] == (not report["same_as_champion"] and all(c["passed"] for c in report["checks"]))
    assert report["minimum_balance"][0]["balance_needed_usd"] > 0
    assert pd.Timestamp(report["walk_forward_picks"][0]["test_from"]) >= frame().index[0] + pd.DateOffset(years=5)


def test_pure_noise_with_a_falling_market_is_not_promoted():
    report = at.evaluate({"XAUUSD": frame(seed=5, drift=-0.0006)}, BASE, CFG)
    assert not report["passed"]


def _report(passed=True, degraded=False):
    block = {"params": at.tuned_values(replace(BASE, rules="ensemble")), "holdout": {"total_return_pct": 4}}
    return {"passed": passed, "same_as_champion": False, "checks": [{"gate": "g", "passed": passed}],
            "challenger": block, "champion_degraded": degraded, "pairs": ["XAUUSD"], "data_to": "x"}


@pytest.mark.parametrize("passed,overrides,expect_file", [
    (False, {}, False), (True, {"auto_apply": False}, False), (True, {}, True)])
def test_promotion_only_when_passed_and_allowed(tmp_path, monkeypatch, passed, overrides, expect_file):
    monkeypatch.setattr(at, "evaluate", lambda *a, **k: _report(passed))
    store = at.Store(tmp_path)
    at.train({}, BASE, store, {**CFG, **overrides}, "test")
    assert store.champion_file.exists() == expect_file


def test_promotions_are_spaced_out_and_can_be_rolled_back(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "evaluate", lambda *a, **k: _report())
    store, start = at.Store(tmp_path), datetime(2026, 1, 1, tzinfo=timezone.utc)
    at.train({}, BASE, store, CFG, "test", now=start)
    second = at.train({}, BASE, store, CFG, "test", now=start + timedelta(days=7))
    assert "only 7 day" in second["decision"]
    assert "removed" in at.rollback(store) and not store.champion_file.exists()
    assert "nothing" in at.rollback(store)


def test_promoted_settings_are_kept_off_live_accounts(tmp_path):
    store = at.Store(tmp_path)
    store.write(store.champion_file, promoted())
    assert at.active_settings(BASE, store, CFG, live_account=False)[0].rules == "ensemble"
    assert at.active_settings(BASE, store, CFG, live_account=True)[0] == BASE
    assert at.active_settings(BASE, store, {**CFG, "auto_apply": False}, False)[0] == BASE


def test_edited_yaml_or_tampered_file_falls_back_to_config():
    assert at.champion_params(BASE, promoted()) is not None
    assert at.champion_params(replace(BASE, sma_len=50), promoted()) is None       # user edited the yaml
    for bad in ({"atr_stop_mult": 50.0}, {"rules": "martingale"}, {"risk_pct": 10}):
        saved = promoted()
        saved["params"] = {**saved["params"], **bad}
        assert at.champion_params(BASE, saved) is None, bad


def test_degraded_results_reduce_risk_even_on_live(tmp_path):
    store = at.Store(tmp_path)
    store.write(store.status_file, {"degraded": True, "config_params": at.tuned_values(BASE)})
    assert at.active_settings(BASE, store, CFG, live_account=True)[1] == 0.5
    assert at.active_settings(replace(BASE, sma_len=50), store, CFG, True)[1] == 1.0   # verdict was for other settings


class FakeBroker:
    def __init__(self, positions):
        self._positions = positions

    def positions(self):
        return self._positions


def _runner(tmp_path, positions, saved=None, scale_status=False):
    store = at.Store(tmp_path)
    store.write(store.champion_file, promoted())
    if scale_status:
        store.write(store.status_file, {"degraded": True, "config_params": at.tuned_values(BASE)})
    cfg = RunnerConfig(pairs=["XAUUSD"], params=BASE, state_file="state.json")
    if saved:
        (tmp_path / "state.json").write_text(json.dumps({"active_params": saved}))
    return FxTrendRunner(FakeBroker(positions), cfg, base_dir=tmp_path, journal=type("J", (), {"add_event": lambda *a: None})(),
                         tuner=at.Tuner(BASE, store, CFG, False))


def test_new_settings_wait_until_no_position_is_open(tmp_path):
    runner = _runner(tmp_path, positions=["open"])
    runner._apply_tuning()
    assert runner.cfg.params == BASE
    runner.broker._positions = []
    runner._apply_tuning()
    assert runner.cfg.params.rules == "ensemble"


def test_restart_with_a_position_restores_the_settings_it_was_opened_under(tmp_path):
    opened_under = at.tuned_values(replace(BASE, rules="momentum", sma_len=50))
    runner = _runner(tmp_path, positions=["open"], saved=opened_under, scale_status=True)
    runner._apply_tuning()
    assert at.tuned_values(runner.cfg.params) == opened_under and runner._risk_scale == 0.5
    assert runner.cfg.params.risk_pct == BASE.risk_pct                     # the base figure is untouched


def test_a_broken_tuner_changes_nothing(tmp_path):
    runner = _runner(tmp_path, positions=[])
    runner.tuner = lambda: 1 / 0
    runner._apply_tuning()
    assert runner.cfg.params == BASE and runner._risk_scale == 1.0


def test_background_training_runs_once_and_reports_failure(tmp_path):
    store, messages, spawned = at.Store(tmp_path), [], []

    class Process:
        code = None

        def poll(self):
            return self.code

    trainer = at.BackgroundTrainer(tmp_path, store, CFG, "cfg.yaml", messages.append,
                                   spawn=lambda: spawned.append(Process()) or spawned[-1])
    trainer.poll()
    trainer.poll()
    assert len(spawned) == 1
    spawned[0].code = 1
    trainer.poll()
    trainer.poll()                                     # failed: not retried within a day
    assert len(spawned) == 1 and "failed" in messages[0]


def test_shipped_config_is_safe():
    y = yaml.safe_load((ROOT / "config" / "fx_trend.yaml").read_text(encoding="utf-8"))["fx_trend"]
    cfg = at.settings(y["auto_train"])
    assert cfg["allow_apply_on_live"] is False and cfg["degraded_risk_scale"] <= 1
    assert all(pair in cfg["costs"] for pair in y["pairs"])


def test_admin_shows_training_status_only_with_the_key(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k-123")
    from fastapi.testclient import TestClient
    from dashboard import admin
    from dashboard.app import app
    admin._fail_log.clear()
    client = TestClient(app)
    assert client.get("/api/admin/autotrain").status_code == 401
    assert set(client.get("/api/admin/autotrain", headers={"X-Dashboard-Key": "k-123"}).json()) == {"status", "report", "promoted"}
    admin._fail_log.clear()
