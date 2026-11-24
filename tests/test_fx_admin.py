import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from core.fx_trend_analytics import flip_analysis, p_touch, performance, project_equity, signal_levels
from core.fx_trend_journal import Journal
from core.fx_trend_supervisor import BotSupervisor, pid_alive
from dashboard.fx_admin import create_app, tail_lines, trade_returns

TOKEN = "t" * 24
H = {"X-Admin-Token": TOKEN}


def trade(i, profit, pair="EURUSD", month="2026-01"):
    return {"ticket": i, "pair": pair, "side": 1, "volume": 0.1, "open_time": f"{month}-01T00:00:00",
            "close_time": f"{month}-{(i % 27) + 1:02d}T00:00:00", "entry": 1.1, "exit": 1.11,
            "profit": profit, "commission": 0.0, "swap": 0.0}


# ---------------- analytics ----------------
def test_p_touch_monotonic_and_bounded():
    assert p_touch(0, 1, 5) == 1.0
    assert p_touch(1, 1, 5) > p_touch(3, 1, 5) > 0
    assert p_touch(1, 1, 20) > p_touch(1, 1, 5)
    assert p_touch(1, 0, 5) == 0.0


def test_flip_probabilities_ordered_by_distance():
    far = flip_analysis(1, 1.20, 0.005, 1.10, 1.05)
    near = flip_analysis(1, 1.101, 0.005, 1.10, 1.05)
    assert near["p_flip_5d"] > far["p_flip_5d"]
    flat = flip_analysis(0, 1.07, 0.005, 1.10, 1.05)
    assert flat["p_flip_5d"] <= flat["p_flip_20d"] <= 1.0


def test_signal_levels_bands():
    closes = pd.Series(np.linspace(1.0, 1.2, 200))
    up, dn = signal_levels(closes, sma=1.1, mom_len=90)
    assert up >= dn


def test_project_equity_needs_data_and_is_ordered():
    assert project_equity([0.01] * 5)["ok"] is False
    rng = np.random.default_rng(0)
    out = project_equity(list(rng.normal(0.002, 0.01, 80)), horizon_trades=30)
    assert out["ok"] and len(out["p50"]) == 30
    assert all(lo <= m <= hi for lo, m, hi in zip(out["p5"], out["p50"], out["p95"]))
    assert 0 <= out["prob_dd_gt_10pct"] <= out["prob_dd_gt_5pct"] <= 1


def test_performance_stats():
    trades = [trade(1, 100), trade(2, -50), trade(3, 30), trade(4, -20, "GBPUSD")]
    eq = [{"equity": 1000}, {"equity": 1100}, {"equity": 990}, {"equity": 1060}]
    p = performance(trades, eq)
    assert p["net_pnl"] == 60 and p["win_rate"] == 0.5 and p["profit_factor"] == pytest.approx(130 / 70, abs=0.01)
    assert p["max_drawdown_pct"] == pytest.approx(-10.0, abs=0.01)
    assert {r["pair"] for r in p["by_pair"]} == {"EURUSD", "GBPUSD"}
    assert performance([], [])["n_trades"] == 0


# ---------------- journal ----------------
def test_journal_roundtrip_and_replace(tmp_path):
    j = Journal(tmp_path / "x.db")
    row = dict(pair="EURUSD", side=1, close=1.1, sma=1.0, mom=0.05, atr=0.005, strength=2.0,
               up_level=1.05, dn_level=1.0, p_flip_5d=0.1, p_flip_20d=0.3)
    j.add_signals("2026-03-03", [row])
    j.add_signals("2026-03-03", [row, {**row, "pair": "GBPUSD"}])   # re-run replaces
    assert len(j.latest_signals()) == 2
    j.upsert_trades([trade(1, 5.0)]); j.upsert_trades([trade(1, 9.0)])
    assert [t["profit"] for t in j.trades()] == [9.0]
    j.add_event("x", "boom", level="ERROR")
    assert j.events(level="ERROR")[0]["message"] == "boom"


# ---------------- supervisor ----------------
def test_supervisor_status_and_flatten_when_stopped(tmp_path):
    s = BotSupervisor(tmp_path)
    st = s.status()
    assert st["running"] is False and st["health"] == "stopped"
    r = s.flatten_and_stop()
    assert r["ok"] and s.kill_file.exists()
    assert "kill file present" in s.start()["message"]       # refuses to start while killed
    s.clear_kill()
    assert not s.kill_file.exists()


def test_pid_alive_self_and_garbage():
    import os
    assert pid_alive(os.getpid()) and not pid_alive(0) and not pid_alive(-5)


def test_supervisor_start_reports_immediate_exit(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/run_fx_trend_bot.py").write_text("import sys; sys.exit(3)")
    r = BotSupervisor(tmp_path).start()
    assert r["ok"] is False and "exited immediately" in r["message"]


# ---------------- dashboard API ----------------
@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path, token=TOKEN)), tmp_path


def test_refuses_weak_token(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(tmp_path, token="short")


def test_auth_required_and_lockout(client):
    c, _ = client
    assert c.get("/").status_code == 200 and c.get("/health").json() == {"ok": True}
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/overview", headers={"X-Admin-Token": "x" * 24}).status_code == 401
    assert c.get("/api/overview", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    for _ in range(6):
        c.get("/api/overview", headers={"X-Admin-Token": "bad"})
    assert c.get("/api/overview", headers=H).status_code == 429       # locked out even with right token


def test_empty_state_endpoints_work(client):
    c, _ = client
    for path in ["overview", "positions", "trades", "signals", "analysis", "predictive",
                 "backtest", "events", "logs?file=errors", "config"]:
        r = c.get(f"/api/{path}", headers=H)
        assert r.status_code == 200, path
    assert c.get("/api/predictive", headers=H).json()["projection"]["ok"] is False
    assert c.get("/api/logs?file=../../etc/passwd", headers=H).status_code == 400


def test_data_flows_to_dashboard(client):
    c, root = client
    j = Journal(root / "data/fx_trend.db")
    for i, eq in enumerate([1000, 1010, 1005]):
        j.add_cycle(f"2026-03-0{i + 1}", eq, 0.1, 0.2, False, 1)
    j.upsert_trades([trade(i, 10 if i % 3 else -8) for i in range(1, 25)])
    (root / "data/fx_trend_snapshot.json").write_text(json.dumps(
        {"ts": "2026-03-03T00:00:00+00:00", "mode": "dry_run", "positions":
         [{"pair": "EURUSD", "side": 1, "profit": 5.0}], "floating_pnl": 5.0, "account": {"equity": 1005}}))
    o = c.get("/api/overview", headers=H).json()
    assert o["performance"]["n_trades"] == 24 and o["snapshot"]["account"]["equity"] == 1005
    assert c.get("/api/positions", headers=H).json()["floating_pnl"] == 5.0
    pred = c.get("/api/predictive", headers=H).json()
    assert pred["projection"]["ok"] and pred["currency_net"] == {"EUR": 1, "USD": -1}


def test_logs_tail_and_filter(client):
    c, root = client
    d = root / "data/logs"; d.mkdir(parents=True)
    (d / "fx_trend_errors.log").write_text(
        "2026-03-03 10:00:00 WARNING x: slow\n2026-03-03 10:01:00 ERROR x: boom\n2026-03-03 10:02:00 INFO x: ok\n")
    r = c.get("/api/logs?file=errors&level=ERROR", headers=H).json()
    assert r["lines"] == ["2026-03-03 10:01:00 ERROR x: boom"]
    assert tail_lines(d / "fx_trend_errors.log", 1) == ["2026-03-03 10:02:00 INFO x: ok"]
    assert tail_lines(d / "nope.log", 5) == []


def test_control_confirmations_and_audit(client):
    c, root = client
    body = lambda **k: {"action": "start", **k}   # noqa: E731
    assert c.post("/api/control", json=body(mode="orders"), headers=H).status_code == 400
    assert c.post("/api/control", json={"action": "flatten"}, headers=H).status_code == 400
    assert c.post("/api/control", json={"action": "nope"}, headers=H).status_code == 400
    assert c.post("/api/control", json={"action": "flatten", "confirm": "FLATTEN"}, headers=H).status_code == 200
    assert (root / "data/fx_trend.KILL").exists()
    r = c.post("/api/control", json={"action": "start"}, headers=H)
    assert r.status_code == 409 and "kill file" in r.json()["message"]       # refuses while killed
    assert c.post("/api/control", json={"action": "clear_kill"}, headers=H).status_code == 200
    kinds = [e["kind"] for e in Journal(root / "data/fx_trend.db").events()]
    assert kinds.count("control") >= 3                                       # every action is audited
    assert c.post("/api/control", json={"action": "stop"}).status_code == 401


def test_trade_returns_use_equity_at_close():
    cycles = [{"bar": "2026-01-01", "equity": 1000}, {"bar": "2026-02-01", "equity": 2000}]
    ts = [{"close_time": "2026-01-15T00:00:00", "profit": 10, "commission": 0, "swap": 0},
          {"close_time": "2026-02-15T00:00:00", "profit": 10, "commission": 0, "swap": 0}]
    assert trade_returns(ts, cycles) == [0.01, 0.005]
