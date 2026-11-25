"""
Admin console v2: kill switch, mode, dry test, engine start, candles, new KPIs.

Properties worth protecting:
  * the kill switch needs a typed word, and once armed a plain resume is refused
  * there is NO way to switch the engine to live from the dashboard
  * the dry test cannot place orders and reports failures instead of hiding them
  * candle data failures degrade to a clean error, never a crash
  * starting the engine twice is refused
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="wamucheha-admin2-")
os.environ["WAMUCHEHA_DB_PATH"] = str(Path(_TMP) / "state.db")
os.environ["CONTROL_DIR"] = str(Path(_TMP) / "control")
os.environ["DASHBOARD_SECRET_KEY"] = "test-secret-123"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import control_channel, dry_test  # noqa: E402
from reporting import analytics  # noqa: E402

H = {"X-Dashboard-Key": "test-secret-123"}


@pytest.fixture()
def client(monkeypatch):
    from dashboard import admin, app as appmod
    admin._fail_log.clear()
    admin.DATA_DIR = Path(_TMP)
    admin.HEARTBEAT = Path(_TMP) / "engine_heartbeat.json"
    admin.LOG_DIR = Path(_TMP) / "logs"
    admin.LOG_DIR.mkdir(exist_ok=True)
    if admin.HEARTBEAT.exists():
        admin.HEARTBEAT.unlink()
    control_channel.poll_commands()
    return TestClient(appmod.app)


# ---------- verbs ----------
def test_new_verbs_are_allow_listed_and_carry_args():
    for v in ("kill", "stop", "dry_run", "dry_test"):
        assert v in control_channel.ALLOWED_COMMANDS
    assert "go_live" not in control_channel.ALLOWED_COMMANDS      # no way to enable real money remotely
    control_channel.send_command("kill", args={"close_positions": True, "evil": {"x": 1}})
    got = control_channel.poll_commands()[0]
    assert got["args"] == {"close_positions": True}                  # non-scalar args dropped


# ---------- kill switch ----------
def test_kill_needs_typed_word(client):
    assert client.post("/api/admin/control", json={"command": "kill"}, headers=H).status_code == 400
    assert client.post("/api/admin/control", json={"command": "kill", "confirm": "kill"}, headers=H).status_code == 400
    r = client.post("/api/admin/control", json={"command": "kill", "confirm": "KILL", "close_positions": True}, headers=H)
    assert r.status_code == 200
    cmd = control_channel.poll_commands()[0]
    assert cmd["command"] == "kill" and cmd["args"]["close_positions"] is True


def test_resume_is_refused_while_kill_switch_armed(client):
    admin_hb = Path(_TMP) / "engine_heartbeat.json"
    admin_hb.write_text(json.dumps({"ts": "2099-01-01T00:00:00+00:00", "health": "halted", "kill_switch": True}))
    r = client.post("/api/admin/control", json={"command": "resume"}, headers=H)
    assert r.status_code == 409 and control_channel.pending_count() == 0
    r = client.post("/api/admin/control", json={"command": "resume", "confirm": "release"}, headers=H)
    assert r.status_code == 200
    assert control_channel.poll_commands()[0]["args"]["release_kill"] is True


def test_resume_normal_when_not_killed(client):
    assert client.post("/api/admin/control", json={"command": "resume"}, headers=H).status_code == 200
    assert "release_kill" not in control_channel.poll_commands()[0]["args"]


def test_stop_needs_confirmation(client):
    assert client.post("/api/admin/control", json={"command": "stop"}, headers=H).status_code == 400
    assert client.post("/api/admin/control", json={"command": "stop", "confirm": "stop"}, headers=H).status_code == 200


def test_cannot_switch_to_live_from_dashboard(client):
    for v in ("live", "go_live", "set_mode"):
        assert client.post("/api/admin/control", json={"command": v, "confirm": v}, headers=H).status_code == 400


# ---------- overview / KPIs ----------
def test_overview_reports_mode_capital_and_rates(client):
    j = client.get("/api/admin/overview", headers=H).json()
    assert "label" in j["mode"] and j["mode"]["kill_switch"] is False
    assert set(j["capital"]) >= {"starting_capital", "balance_now", "paid_out", "total_value", "return_pct"}


def test_mode_reflects_engine_heartbeat(client):
    (Path(_TMP) / "engine_heartbeat.json").write_text(json.dumps(
        {"ts": "2099-01-01T00:00:00+00:00", "health": "running", "mode": "DRY-RUN (simulated)",
         "live": False, "kill_switch": True, "markets": ["mt5:XAUUSD"]}))
    m = client.get("/api/admin/overview", headers=H).json()["mode"]
    assert m["kill_switch"] is True and m["markets"] == ["mt5:XAUUSD"] and m["live"] is False


def test_metrics_include_loss_rate_gross_and_efficiency():
    t = [{"pnl": p, "fees": 0.0, "closed_at": f"2025-01-0{i + 1}T10:00:00+00:00",
          "opened_at": f"2025-01-0{i + 1}T09:00:00+00:00"} for i, p in enumerate([3.0, -1.0, 2.0, -1.0, 0.0])]
    m = analytics.compute_metrics(t, 100.0)
    assert m["win_rate_pct"] == 40.0 and m["loss_rate_pct"] == 40.0 and m["breakeven"] == 1
    assert m["gross_profit"] == 5.0 and m["gross_loss"] == -2.0
    assert m["efficiency_pct"] == 60.0            # net 3 of gross 5 wins kept


# ---------- engine start ----------
def test_start_refused_when_engine_already_running(client):
    from datetime import datetime, timezone
    (Path(_TMP) / "engine_heartbeat.json").write_text(json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "health": "running"}))
    r = client.post("/api/admin/engine/start", headers=H)
    assert r.status_code == 409


def test_start_requires_control_key(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "change_me")
    assert client.post("/api/admin/engine/start", headers={"X-Dashboard-Key": "change_me"}).status_code == 403


def test_start_launches_detached_process_when_offline(client, monkeypatch):
    from dashboard import admin
    launched = {}

    class P:
        pid = 4242

    monkeypatch.setattr(admin.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd, kw=kw) or P())
    monkeypatch.setattr(admin, "PID_FILE", Path(_TMP) / "engine.pid")
    r = client.post("/api/admin/engine/start", headers=H)
    assert r.status_code == 200 and r.json()["pid"] == 4242
    assert launched["cmd"][-1] == "main.py"
    assert not (Path(_TMP) / "engine.pid").read_text() == ""


# ---------- candles ----------
def _fake(symbol, tf, limit, source):
    base = 1_700_000_000
    return {"symbol": symbol, "timeframe": tf, "source": "fake", "last_price": 101.0,
            "fallback_notes": [], "candles": [{"time": base + i * 900, "open": 100, "high": 102,
                                               "low": 99, "close": 101, "volume": 5} for i in range(30)]}


def test_candles_endpoint_returns_candles_and_requires_key(client, monkeypatch):
    from dashboard import market_data
    monkeypatch.setattr(market_data, "fetch_candles", _fake)
    assert client.get("/api/admin/candles", params={"symbol": "BTC/USDT"}).status_code == 401
    j = client.get("/api/admin/candles", params={"symbol": "BTC/USDT", "tf": "15m"}, headers=H).json()
    assert len(j["candles"]) == 30 and j["source"] == "fake" and j["markers"] == []


def test_candles_failure_is_a_clean_502_and_bad_input_400(client, monkeypatch):
    from dashboard import market_data

    def boom(*a, **k):
        raise RuntimeError("all sources down")

    monkeypatch.setattr(market_data, "fetch_candles", boom)
    assert client.get("/api/admin/candles", params={"symbol": "BTC/USDT"}, headers=H).status_code == 502
    assert client.get("/api/admin/candles", params={"symbol": "../../etc"}, headers=H).status_code == 400
    monkeypatch.setattr(market_data, "fetch_candles", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad tf")))
    assert client.get("/api/admin/candles", params={"symbol": "BTC/USDT", "tf": "9x"}, headers=H).status_code == 400


def test_market_data_rejects_bad_timeframe_and_falls_through_providers(monkeypatch):
    from dashboard import market_data
    with pytest.raises(ValueError):
        market_data.fetch_candles("BTC/USDT", "7m")
    market_data._cache.clear()
    monkeypatch.setattr(market_data, "_from_mt5", lambda *a: (_ for _ in ()).throw(RuntimeError("no mt5")))
    monkeypatch.setattr(market_data, "_from_ccxt", lambda *a: ([{"time": 2, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1},
                                                                {"time": 1, "open": 1, "high": 2, "low": 1, "close": 1, "volume": 1}], "binance"))
    r = market_data.fetch_candles("BTC/USDT", "15m", 50)
    assert r["source"].startswith("binance") and [c["time"] for c in r["candles"]] == [1, 2]   # sorted
    assert any("mt5" in n for n in r["fallback_notes"])


# ---------- dry test ----------
class _Ex:
    def __init__(self, dry=True):
        self.dry_run = dry
        self.orders = 0

    def open_trade(self, *a, **k):
        self.orders += 1


class _Feed:
    def __init__(self, rows=120, fail=False):
        self.rows, self.fail = rows, fail

    def get_ohlcv(self, symbol, tf, limit):
        if self.fail:
            raise RuntimeError("feed down")
        return pd.DataFrame({"close": [1.0] * self.rows})


def test_dry_test_places_no_orders_and_passes_when_healthy():
    ex = _Ex()
    res = dry_test.run_dry_test({"mt5": ex}, _Feed(), [("mt5", "XAUUSD")],
                                lambda s: {"side": "buy", "score": 0.7, "strategies": ["ema"], "amount": 1}, "DRY-RUN")
    assert res["passed"] and ex.orders == 0
    assert dry_test.read_dry_test()["passed"] is True


def test_dry_test_reports_every_failure():
    res = dry_test.run_dry_test({}, _Feed(fail=True), [("mt5", "XAUUSD")], lambda s: None, "DRY-RUN")
    assert not res["passed"]
    names = {c["name"] for c in res["checks"] if not c["ok"]}
    assert "Executors" in names and "Data mt5:XAUUSD" in names

    res = dry_test.run_dry_test({"mt5": _Ex()}, _Feed(fail=True), [("mt5", "XAUUSD")], lambda s: None, "DRY-RUN")
    assert any(c["name"] == "Data mt5:XAUUSD" and not c["ok"] for c in res["checks"])
    res = dry_test.run_dry_test({"mt5": _Ex()}, _Feed(rows=10), [("mt5", "XAUUSD")], lambda s: None, "DRY-RUN")
    assert any("candles" in c["detail"] and not c["ok"] for c in res["checks"])

    def bad_signal(s):
        raise ValueError("strategy exploded")

    res = dry_test.run_dry_test({"mt5": _Ex()}, _Feed(), [("mt5", "XAUUSD")], bad_signal, "DRY-RUN")
    assert any(c["name"].startswith("Signal") and not c["ok"] for c in res["checks"])


def test_dry_test_warns_when_executor_is_live():
    res = dry_test.run_dry_test({"mt5": _Ex(dry=False)}, _Feed(), [], lambda s: None, "LIVE")
    live = next(c for c in res["checks"] if c["name"] == "Executor: mt5")
    assert live["level"] == "warn" and "REAL" in live["detail"]


def test_dry_test_endpoint(client):
    dry_test.run_dry_test({"mt5": _Ex()}, _Feed(), [("mt5", "XAUUSD")], lambda s: None, "DRY-RUN")
    j = client.get("/api/admin/dry-test", headers=H).json()
    assert j["checks"] and "summary" in j
    assert client.get("/api/admin/dry-test").status_code == 401


def test_no_remote_shell_endpoint_exists(client):
    """The old /api/terminal ran arbitrary shell commands; it must never come back."""
    from dashboard import app as appmod
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    assert not any("terminal" in p for p in paths)
    assert client.post("/api/terminal", json={"command": "whoami"}, headers=H).status_code in (404, 405)


# ---------- demo test trade ----------
def test_test_trade_needs_demo_mode_and_typed_word(client):
    assert "test_trade" in control_channel.ALLOWED_COMMANDS
    # not in demo mode: refused even with the right word
    r = client.post("/api/admin/control", json={"command": "test_trade", "confirm": "TEST"}, headers=H)
    assert r.status_code == 409 and control_channel.pending_count() == 0
    (Path(_TMP) / "engine_heartbeat.json").write_text(json.dumps(
        {"ts": "2099-01-01T00:00:00+00:00", "health": "running", "mt5_demo": True}))
    assert client.post("/api/admin/control", json={"command": "test_trade"}, headers=H).status_code == 400
    assert client.post("/api/admin/control", json={"command": "test_trade", "confirm": "test"}, headers=H).status_code == 400
    r = client.post("/api/admin/control", json={"command": "test_trade", "confirm": "TEST"}, headers=H)
    assert r.status_code == 200 and control_channel.poll_commands()[0]["command"] == "test_trade"


def test_mt5_demo_flag_is_honoured_only_for_demo_servers():
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert '"demo" in mt5_server.lower()' in src
    assert "dry_run=not (LIVE_TRADING or mt5_demo_orders)" in src


# ---------- Start inside Docker ----------
def _hb(state_health, **extra):
    from datetime import datetime, timezone
    (Path(_TMP) / "engine_heartbeat.json").write_text(json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "health": state_health, **extra}))


def test_start_in_docker_resumes_a_paused_engine(client, monkeypatch):
    from dashboard import admin
    monkeypatch.setattr(admin, "_in_container", lambda: True)
    _hb("standby")
    r = client.post("/api/admin/engine/start", headers=H)
    assert r.status_code == 200 and r.json()["via"] == "resume"
    assert control_channel.poll_commands()[0]["command"] == "resume"


def test_start_in_docker_explains_when_already_running_or_offline(client, monkeypatch):
    from dashboard import admin
    monkeypatch.setattr(admin, "_in_container", lambda: True)
    _hb("running")
    r = client.post("/api/admin/engine/start", headers=H)
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    (Path(_TMP) / "engine_heartbeat.json").unlink()
    r = client.post("/api/admin/engine/start", headers=H)
    assert r.status_code == 409 and "docker compose up -d engine" in r.json()["detail"]
    assert control_channel.pending_count() == 0


def test_start_in_docker_will_not_bypass_the_kill_switch(client, monkeypatch):
    from dashboard import admin
    monkeypatch.setattr(admin, "_in_container", lambda: True)
    _hb("halted", kill_switch=True)
    r = client.post("/api/admin/engine/start", headers=H)
    assert r.status_code == 409 and "kill switch" in r.json()["detail"]
    assert control_channel.pending_count() == 0


# ---------- terminal data: /live and /ledger ----------
def _seed_trades():
    from core.state_manager import StateManager
    sm = StateManager(stake_amount=1.0, initial_trading_balance=1000.0)
    for i, (sym, pnl) in enumerate([("XAUUSD", 5.0), ("BTC/USDT", -2.0), ("XAUUSD", 3.0)]):
        cid = f"led-{sym}-{i}"
        sm.record_trade_open(cid, "mt5" if sym == "XAUUSD" else "okx", sym, "buy", 0.01, 100.0, 95.0, 110.0, ["ema"], 0.5, "trend")
        sm.record_trade_close(cid, 101.0, pnl, "tp" if pnl > 0 else "sl")
    return sm


def test_ledger_has_running_balance_and_asset_summary(client):
    _seed_trades()
    j = client.get("/api/admin/ledger", headers=H).json()
    rows = list(reversed(j["trades"]))           # API returns newest first
    assert len(rows) >= 3
    bal = j["start_balance"]
    for r in rows:                                # every row's balance_after = previous + its pnl
        bal = round(bal + r["pnl"], 4)
        assert r["balance_after"] == bal
    assert abs(j["balance"] - rows[-1]["balance_after"]) < 1e-6
    assert j["summary"]["gold"]["trades"] >= 2 and j["summary"]["crypto"]["trades"] >= 1
    assert client.get("/api/admin/ledger").status_code == 401


def test_live_returns_prices_positions_and_floating_pnl(client, monkeypatch):
    from dashboard import market_data
    from core.state_manager import StateManager
    sm = StateManager(stake_amount=1.0, initial_trading_balance=1000.0)
    sm.record_trade_open("live-open-1", "mt5", "XAUUSD", "buy", 0.01, 3500.0, 3430.0, 3590.0, ["ema"], 0.5, "trend")
    sm.record_trade_open("live-open-2", "okx", "BTC/USDT", "sell", 0.5, 60000.0, 61000.0, 58000.0, ["ema"], 0.5, "trend")
    prices = {"XAUUSD": 3510.0, "BTC/USDT": 59000.0}
    monkeypatch.setattr(market_data, "snapshots", lambda syms: [
        {"symbol": s, "price": prices.get(s), "change_pct": 1.2, "source": "fake"} for s in syms])
    j = client.get("/api/admin/live", headers=H).json()
    pos = {p["symbol"]: p for p in j["positions"]}
    assert abs(pos["XAUUSD"]["pnl"] - 10.0) < 1e-6          # 0.01 lot * 100 oz * (3510 - 3500)
    assert abs(pos["BTC/USDT"]["pnl"] - 500.0) < 1e-6        # short 0.5 BTC, price fell 1000
    assert abs(j["open_pnl"] - 510.0) < 1e-6 and abs(j["equity"] - (j["balance"] + 510.0)) < 1e-6
    assert any(r["symbol"] == "XAUUSD" and r["asset"] == "gold" for r in j["prices"])
    assert client.get("/api/admin/live").status_code == 401


def test_live_survives_a_dead_price_feed(client, monkeypatch):
    from dashboard import market_data
    monkeypatch.setattr(market_data, "snapshots", lambda syms: [{"symbol": s, "price": None, "error": "down"} for s in syms])
    assert client.get("/api/admin/live", headers=H).status_code == 200
