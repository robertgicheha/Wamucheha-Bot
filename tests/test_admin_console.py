"""
Tests for the admin console: command mailbox, analytics/projection, error
capture and the authenticated API.

Properties worth protecting:
  * only allow-listed verbs ever reach the engine; stale/garbled files are dropped
  * every admin route needs the key; control needs a NON-default key
  * restart/standby need the typed confirmation
  * the log viewer cannot be pointed outside the whitelisted log files
  * projection refuses to look authoritative on a tiny sample
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="wamucheha-admin-")
os.environ["WAMUCHEHA_DB_PATH"] = str(Path(_TMP) / "state.db")
os.environ["CONTROL_DIR"] = str(Path(_TMP) / "control")
os.environ["DASHBOARD_SECRET_KEY"] = "test-secret-123"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import control_channel  # noqa: E402
from reporting import analytics  # noqa: E402


def _trades(pnls):
    return [{"pnl": p, "fees": 0.01, "symbol": "BTC/USDT" if i % 2 else "ETH/USDT",
             "exchange": "okx", "regime": "trend", "strategies": '["ema","rsi"]',
             "opened_at": f"2025-01-{1 + i % 28:02d}T10:00:00+00:00",
             "closed_at": f"2025-01-{1 + i % 28:02d}T1{i % 9}:30:00+00:00", "status": "closed"}
            for i, p in enumerate(pnls)]


# ---------- control channel ----------
def test_command_roundtrip_and_consumed_once():
    control_channel.send_command("halt", by="t")
    got = control_channel.poll_commands()
    assert [c["command"] for c in got] == ["halt"]
    assert control_channel.poll_commands() == []


def test_unknown_verb_rejected_at_send_and_at_poll():
    with pytest.raises(ValueError):
        control_channel.send_command("rm -rf /")
    (control_channel.CONTROL_DIR / "cmd_1_x.json").write_text('{"command":"drop_db","ts":%f}' % time.time())
    assert control_channel.poll_commands() == []


def test_stale_and_garbled_commands_dropped():
    d = control_channel.CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmd_1_old.json").write_text('{"command":"restart","ts":%f}' % (time.time() - 3600))
    (d / "cmd_2_bad.json").write_text("{not json")
    assert control_channel.poll_commands() == []
    assert control_channel.pending_count() == 0


def test_commands_come_back_oldest_first():
    control_channel.send_command("halt")
    time.sleep(0.01)
    control_channel.send_command("resume")
    assert [c["command"] for c in control_channel.poll_commands()] == ["halt", "resume"]


# ---------- analytics ----------
def test_metrics_basic():
    m = analytics.compute_metrics(_trades([10, -5, 10, -5, 10]), starting_balance=100)
    assert m["trades"] == 5 and m["wins"] == 3
    assert m["total_pnl"] == 20
    assert m["profit_factor"] == 3.0
    assert m["max_loss_streak"] == 1 and m["max_win_streak"] == 1
    assert m["max_drawdown"] == -5


def test_metrics_empty_is_safe():
    assert analytics.compute_metrics([]) == {"trades": 0}


def test_breakdown_groups_by_symbol_and_strategy():
    b = analytics.breakdowns(_trades([5, -1, 5, -1]))
    assert {r["key"] for r in b["by_symbol"]} == {"BTC/USDT", "ETH/USDT"}
    assert {r["key"] for r in b["by_strategy"]} == {"ema", "rsi"}


def test_projection_flags_small_sample_unreliable():
    p = analytics.project_equity(_trades([1, -1, 2]), balance=100)
    assert p["reliable"] is False and "need" in p["note"]


def test_projection_deterministic_and_sane():
    t = _trades([2.0, -1.0] * 30)
    a = analytics.project_equity(t, 1000, horizon_trades=40)
    b = analytics.project_equity(t, 1000, horizon_trades=40)
    assert a == b and a["reliable"] is True
    assert a["bands"][-1]["p5"] <= a["bands"][-1]["p50"] <= a["bands"][-1]["p95"]
    assert a["edge_is_positive_with_95pct_confidence"] is True


def test_projection_negative_edge_not_called_positive():
    p = analytics.project_equity(_trades([-1.0, 0.5] * 30), 1000)
    assert p["edge_is_positive_with_95pct_confidence"] is False
    assert p["prob_loss_pct"] > 50


# ---------- error capture ----------
def test_error_capture_records_only_error_lines(monkeypatch):
    from core import structured_logger as sl
    monkeypatch.setattr(sl, "ERROR_LOG", Path(_TMP) / "errors.log")
    import io
    t = sl._TeeStream(io.StringIO())
    t.write("all fine here\n")
    t.write("NSE analysis error: boom\n")
    txt = (Path(_TMP) / "errors.log").read_text()
    assert "boom" in txt and "all fine" not in txt


# ---------- API ----------
@pytest.fixture(scope="module")
def client():
    from dashboard import app as appmod
    from dashboard import admin
    admin.LOG_DIR = Path(_TMP) / "logs"
    admin.LOG_DIR.mkdir(exist_ok=True)
    (admin.LOG_DIR / "errors.log").write_text(
        "2025-01-01T00:00:00.000000+00:00 Exchange error: timeout 123\n"
        "2025-01-01T00:01:00.000000+00:00 Exchange error: timeout 456\n")
    return TestClient(appmod.app)


@pytest.fixture(autouse=True)
def _reset_limiter():
    from dashboard import admin
    admin._fail_log.clear()


H = {"X-Dashboard-Key": "test-secret-123"}


@pytest.mark.parametrize("path", ["/overview", "/errors", "/analytics", "/projection",
                                  "/logs/files", "/config", "/audit", "/health"])
def test_routes_require_key(client, path):
    assert client.get("/api/admin" + path).status_code == 401
    assert client.get("/api/admin" + path, headers={"X-Dashboard-Key": "wrong"}).status_code == 401


def test_admin_page_served(client):
    r = client.get("/admin")
    assert r.status_code == 200 and "Admin Console" in r.text


def test_overview_and_analytics_work_on_empty_db(client):
    assert client.get("/api/admin/overview", headers=H).status_code == 200
    assert client.get("/api/admin/analytics", headers=H).status_code == 200
    assert client.get("/api/admin/projection", headers=H).json()["reliable"] is False


def test_errors_groups_repeats(client):
    j = client.get("/api/admin/errors", headers=H).json()
    assert j["total_in_window"] == 2
    assert j["top_errors"][0]["count"] == 2     # digits normalised: same signature


def test_log_tail_rejects_path_traversal(client):
    for bad in ("../../etc/passwd", "..\\..\\config\\config.yaml", "state.db", "/etc/passwd"):
        assert client.get("/api/admin/logs/tail", params={"file": bad}, headers=H).status_code == 404
    assert client.get("/api/admin/logs/tail", params={"file": "errors.log"}, headers=H).status_code == 200


def test_control_needs_confirmation_for_disruptive(client):
    r = client.post("/api/admin/control", json={"command": "restart"}, headers=H)
    assert r.status_code == 400
    r = client.post("/api/admin/control", json={"command": "restart", "confirm": "restart"}, headers=H)
    assert r.status_code == 200 and control_channel.poll_commands()[0]["command"] == "restart"


def test_control_rejects_unknown_command(client):
    assert client.post("/api/admin/control", json={"command": "format_disk"}, headers=H).status_code == 400


def test_control_disabled_with_default_secret(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "change_me")
    r = client.post("/api/admin/control", json={"command": "halt"},
                    headers={"X-Dashboard-Key": "change_me"})
    assert r.status_code == 403
    assert control_channel.pending_count() == 0


def test_brute_force_is_rate_limited(client):
    from dashboard import admin
    admin._fail_log.clear()
    codes = [client.get("/api/admin/ping", headers={"X-Dashboard-Key": "x"}).status_code
             for _ in range(12)]
    assert 429 in codes
    admin._fail_log.clear()
