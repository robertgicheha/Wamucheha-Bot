"""
Admin console API (mounted by dashboard/app.py under /api/admin, UI at /admin).

Everything here is behind X-Dashboard-Key. Control actions (halt / resume /
standby / restart) go through core.control_channel: the dashboard only writes a
command file, the engine executes it and records the audit trail. The
dashboard never touches the trading DB or Docker itself.
"""
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from core import control_channel, dry_test
from dashboard import market_data
from core.state_manager import SNAPSHOT_PATH
from reporting import analytics

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
HEARTBEAT = DATA_DIR / "engine_heartbeat.json"
STALE_AFTER = 180        # heartbeat is written every 60s; 3 missed beats = stale
OFFLINE_AFTER = 600

router = APIRouter(prefix="/api/admin")

# ---------- auth ----------
_fail_log = defaultdict(deque)   # ip -> timestamps of recent failed attempts
_MAX_FAILS, _WINDOW = 10, 60


def _secret() -> str:
    return os.environ.get("DASHBOARD_SECRET_KEY", "change_me")


def require_key(request: Request, x_dashboard_key: str = Header(None)):
    ip = request.client.host if request.client else "?"
    q = _fail_log[ip]
    now = time.time()
    while q and now - q[0] > _WINDOW:
        q.popleft()
    if len(q) >= _MAX_FAILS:
        raise HTTPException(429, "too many failed attempts; wait a minute")
    if not x_dashboard_key or not hmac.compare_digest(x_dashboard_key.encode(), _secret().encode()):
        q.append(now)
        raise HTTPException(401, "unauthorized")


def require_control_key(request: Request, x_dashboard_key: str = Header(None)):
    require_key(request, x_dashboard_key)
    if _secret() in ("", "change_me"):
        raise HTTPException(403, "control actions are disabled until DASHBOARD_SECRET_KEY "
                                 "is changed from its default")


# ---------- helpers ----------
def _state():
    from dashboard import app as appmod
    return appmod._get_state()


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default if default is not None else {}


def _tail(path: Path, n: int, max_bytes: int = 2_000_000) -> list:
    """Last n lines without reading a whole large file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def _closed_trades(limit=5000) -> list:
    try:
        return [t for t in _state().get_recent_trades(limit) if t.get("status") == "closed"]
    except Exception:
        return []


def _engine_status() -> dict:
    hb = _read_json(HEARTBEAT)
    ts = hb.get("ts")
    age = None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        pass
    if age is None:
        level = "unknown"
    elif age > OFFLINE_AFTER:
        level = "offline"
    elif age > STALE_AFTER:
        level = "stale"
    else:
        level = hb.get("health", "running")
    return {"state": level, "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "uptime_seconds": hb.get("uptime_seconds"), "restarts": hb.get("restarts"),
            "run_started": hb.get("run_started")}


_NORMALISE = re.compile(r"\d[\d.,:\-T+]*")


def _signature(line: str) -> str:
    return _NORMALISE.sub("#", line)[:160]


# ---------- read endpoints ----------
@router.get("/ping", dependencies=[Depends(require_key)])
def ping():
    return {"ok": True, "control_enabled": _secret() not in ("", "change_me")}


@router.get("/overview", dependencies=[Depends(require_key)])
def overview():
    snap = _read_json(SNAPSHOT_PATH)
    trades = _closed_trades()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if str(t.get("closed_at", "")).startswith(today)]
    try:
        open_pos = _state().get_open_positions()
    except Exception:
        open_pos = []
    err_lines = _tail(LOG_DIR / "errors.log", 2000)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    errors_24h = sum(1 for l in err_lines if l[:32] >= cutoff)
    hb = _read_json(HEARTBEAT)
    try:
        flows = _state().get_cash_flow_totals()
    except Exception:
        flows = {}
    metrics = analytics.compute_metrics(trades, snap.get("trading_balance") or 0.0)
    balance = snap.get("trading_balance") or 0.0
    paid_out = flows.get("swept", 0.0) + flows.get("returned", 0.0)
    start_cap = round(balance - (metrics.get("total_pnl") or 0.0) + flows.get("swept", 0.0), 4)
    capital = {
        "starting_capital": start_cap,                       # derived: what the account began with
        "balance_now": round(balance, 4),                    # after all closed trades and payouts
        "paid_out": round(paid_out, 4),                      # profit already moved out to you
        "total_value": round(balance + paid_out, 4),         # balance + what you already took out
        "return_pct": round(((balance + paid_out) / start_cap - 1) * 100, 2) if start_cap else None,
    }
    live_env = os.environ.get("LIVE_TRADING", "false").lower() == "true"
    return {
        "mode": {"label": hb.get("mode") or ("LIVE - REAL MONEY" if live_env else "DRY-RUN (simulated)"),
                 "live": bool(hb.get("live")), "kill_switch": bool(hb.get("kill_switch")),
                 "markets": hb.get("markets", [])},
        "capital": capital,
        "engine": _engine_status(),
        "risk": {k: snap.get(k) for k in ("trading_balance", "peak_balance", "daily_pnl",
                                          "consecutive_losses", "trading_halted", "halt_reason",
                                          "updated_at")},
        "open_positions": len(open_pos),
        "today": {"trades": len(today_trades),
                  "pnl": round(sum((t.get("pnl") or 0) for t in today_trades), 4)},
        "metrics": metrics,
        "errors_24h": errors_24h,
        "pending_commands": control_channel.pending_count(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health", dependencies=[Depends(require_key)])
def health_checks():
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    eng = _engine_status()
    add("Engine heartbeat", eng["state"] in ("running", "halted", "standby", "starting"),
        f"{eng['state']} (last beat {eng['heartbeat_age_seconds']}s ago)"
        if eng["heartbeat_age_seconds"] is not None else f"{eng['state']} (no heartbeat file: engine never started?)")
    try:
        du = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else ROOT)
        add("Disk space", du.free / du.total > 0.10, f"{du.free / 1e9:.1f} GB free ({du.free / du.total:.0%})")
    except Exception as e:
        add("Disk space", False, str(e))
    db = DATA_DIR / "state.db"
    add("State database", db.exists(), f"{db.stat().st_size / 1e6:.1f} MB" if db.exists() else "missing")
    snap = _read_json(SNAPSHOT_PATH)
    add("State snapshot", bool(snap), snap.get("updated_at", "missing"))
    add("Control channel", os.access(control_channel.CONTROL_DIR.parent, os.W_OK)
        if control_channel.CONTROL_DIR.parent.exists() else False,
        str(control_channel.CONTROL_DIR))
    add("Dashboard secret changed", _secret() not in ("", "change_me"),
        "control actions enabled" if _secret() not in ("", "change_me") else "still default")
    return {"checks": checks, "ok": all(c["ok"] for c in checks)}


@router.get("/errors", dependencies=[Depends(require_key)])
def errors(n: int = 200, q: str = ""):
    n = max(1, min(n, 2000))
    lines = _tail(LOG_DIR / "errors.log", 5000)
    if q:
        lines = [l for l in lines if q.lower() in l.lower()]
    sigs = Counter(_signature(l[33:] if len(l) > 33 else l) for l in lines)
    now = datetime.now(timezone.utc)
    hours = Counter()
    for l in lines:
        try:
            hours[(now - datetime.fromisoformat(l[:32].strip())).total_seconds() // 3600] += 1
        except Exception:
            pass
    return {
        "total_in_window": len(lines),
        "lines": list(reversed(lines[-n:])),
        "top_errors": [{"message": s, "count": c} for s, c in sigs.most_common(10)],
        "per_hour_last_24h": [hours.get(float(h), 0) for h in range(23, -1, -1)],
    }


def _log_files() -> dict:
    files = {"errors.log": LOG_DIR / "errors.log", "events.log": DATA_DIR / "events.log",
             "trade_log.jsonl": DATA_DIR / "trade_log.jsonl"}
    if LOG_DIR.exists():
        for p in sorted(LOG_DIR.glob("bot_*.jsonl"), reverse=True)[:14]:
            files[p.name] = p
    return {k: v for k, v in files.items() if v.exists()}


@router.get("/logs/files", dependencies=[Depends(require_key)])
def log_files():
    return {"files": [{"name": k, "size": v.stat().st_size} for k, v in _log_files().items()]}


@router.get("/logs/tail", dependencies=[Depends(require_key)])
def log_tail(file: str = "errors.log", n: int = 300, q: str = ""):
    path = _log_files().get(file)          # whitelist lookup: no path traversal possible
    if path is None:
        raise HTTPException(404, "unknown log file")
    lines = _tail(path, max(1, min(n, 3000)) if not q else 5000)
    if q:
        lines = [l for l in lines if q.lower() in l.lower()][-max(1, min(n, 3000)):]
    return {"file": file, "lines": lines}


@router.get("/analytics", dependencies=[Depends(require_key)])
def analytics_view():
    snap = _read_json(SNAPSHOT_PATH)
    trades = _closed_trades()
    return {"metrics": analytics.compute_metrics(trades, snap.get("trading_balance") or 0.0),
            "breakdowns": analytics.breakdowns(trades),
            "trend": analytics.equity_trend(trades),
            "equity_curve": _state().get_equity_curve(500)}


@router.get("/projection", dependencies=[Depends(require_key)])
def projection(horizon: int = 50, simulations: int = 2000):
    snap = _read_json(SNAPSHOT_PATH)
    limit = 5.0
    try:
        import yaml
        limit = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["risk"]["max_drawdown_pct"]
    except Exception:
        pass
    return analytics.project_equity(_closed_trades(), snap.get("trading_balance") or 0.0,
                                    horizon_trades=max(5, min(horizon, 500)),
                                    simulations=max(200, min(simulations, 10000)),
                                    max_drawdown_limit_pct=limit)


@router.get("/trades", dependencies=[Depends(require_key)])
def trades(n: int = 100):
    return {"trades": _state().get_recent_trades(max(1, min(n, 1000)))}


@router.get("/positions", dependencies=[Depends(require_key)])
def positions():
    return {"positions": _state().get_open_positions()}


@router.get("/signals", dependencies=[Depends(require_key)])
def signals(n: int = 100):
    out = []
    files = sorted(LOG_DIR.glob("bot_*.jsonl"), reverse=True)[:2] if LOG_DIR.exists() else []
    for f in files:
        for line in reversed(_tail(f, 3000)):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") in ("signal", "risk_event", "system"):
                out.append(e)
            if len(out) >= n:
                return {"events": out}
    return {"events": out}


@router.get("/config", dependencies=[Depends(require_key)])
def config_view():
    """Read-only effective config. Secrets live in .env, never in this file."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    except Exception as e:
        raise HTTPException(500, f"config unreadable: {e}")
    ex = [{"name": e.get("name"), "enabled": e.get("enabled"), "markets": e.get("markets", [])}
          for e in cfg.get("execution", {}).get("exchanges", [])]
    return {"risk": cfg.get("risk"), "fees": cfg.get("fees"), "account": {
            k: v for k, v in cfg.get("account", {}).items() if k != "stake_wallet_address"},
            "exchanges": ex, "mt5_markets": cfg.get("execution", {}).get("mt5_markets", []),
            "live_trading": os.environ.get("LIVE_TRADING", "false")}


@router.get("/audit", dependencies=[Depends(require_key)])
def audit(n: int = 50):
    return {"entries": control_channel.read_audit(n), "pending": control_channel.pending_count()}


# ---------- control ----------
class ControlBody(BaseModel):
    command: str
    confirm: str = ""
    note: str = ""
    close_positions: bool = False


# verbs that need the operator to type a word: a mis-click must not do these
_CONFIRM_WORD = {"restart": "restart", "standby": "standby", "stop": "stop", "kill": "KILL"}


@router.post("/control")
def control(body: ControlBody, request: Request, _=Depends(require_control_key)):
    if body.command not in control_channel.ALLOWED_COMMANDS:
        raise HTTPException(400, f"command must be one of {sorted(control_channel.ALLOWED_COMMANDS)}")
    need = _CONFIRM_WORD.get(body.command)
    if need and body.confirm != need:
        raise HTTPException(400, f"type '{need}' in the confirm field to proceed")
    args = {}
    if body.command == "kill":
        args["close_positions"] = bool(body.close_positions)
    if body.command == "resume" and _read_json(HEARTBEAT).get("kill_switch"):
        # A kill switch is a deliberate lock: releasing it takes a second, explicit confirmation.
        if body.confirm != "release":
            raise HTTPException(409, "the kill switch is armed; type 'release' to resume trading")
        args["release_kill"] = True
    who = f"dashboard@{request.client.host if request.client else '?'}"
    sent = control_channel.send_command(body.command, by=who, note=body.note, args=args)
    return {"queued": True, "command": sent,
            "note": "The engine applies this on its next loop tick (seconds). If the engine "
                    f"is offline the command is discarded after "
                    f"{control_channel.MAX_COMMAND_AGE_SECONDS // 60} minutes."}


# ---------- start the engine (native runs) ----------
PID_FILE = DATA_DIR / "engine.pid"


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                 text=True, timeout=5).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


@router.post("/engine/start")
def engine_start(request: Request, _=Depends(require_control_key)):
    """Launch main.py as a detached process. Only for native (non-container) runs; in
    Docker the engine is its own supervised service and is started with docker compose."""
    if Path("/.dockerenv").exists():
        raise HTTPException(400, "running in a container: start the engine service with "
                                 "`docker compose up -d engine` instead")
    status = _engine_status()
    if status["state"] in ("running", "halted", "standby", "starting", "restarting"):
        raise HTTPException(409, f"engine is already {status['state']}")
    try:
        if PID_FILE.exists() and _pid_alive(int(PID_FILE.read_text().strip() or 0)):
            raise HTTPException(409, "an engine process started from here is still alive")
    except ValueError:
        pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / "engine_stdout.log", "ab")
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), stdout=out, stderr=out,
                            stdin=subprocess.DEVNULL, creationflags=flags,
                            start_new_session=(os.name != "nt"))
    PID_FILE.write_text(str(proc.pid))
    control_channel.append_audit({"command": "start",
                                  "by": f"dashboard@{request.client.host if request.client else '?'}",
                                  "id": f"pid{proc.pid}"})
    return {"started": True, "pid": proc.pid,
            "note": "Engine launching. It appears as running in about a minute; output goes to "
                    "data/logs/engine_stdout.log."}


# ---------- dry test + market data ----------
@router.get("/dry-test", dependencies=[Depends(require_key)])
def dry_test_result():
    return dry_test.read_dry_test() or {"checks": [], "summary": "No dry test has been run yet."}


@router.get("/symbols", dependencies=[Depends(require_key)])
def symbols():
    mt5 = []
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
        mt5 = list(cfg.get("execution", {}).get("mt5_markets", []))
    except Exception:
        pass
    return {"symbols": market_data.default_symbols(mt5), "timeframes": market_data.TIMEFRAMES,
            "traded": mt5}


@router.get("/candles", dependencies=[Depends(require_key)])
def candles(symbol: str, tf: str = "15m", limit: int = 300, source: str = "auto"):
    if not re.fullmatch(r"[A-Za-z0-9]{2,12}(/[A-Za-z0-9]{2,12})?([._-][A-Za-z0-9]{1,8})?", symbol):
        raise HTTPException(400, "bad symbol")
    try:
        data = market_data.fetch_candles(symbol, tf, limit, source)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"no data source could provide {symbol}: {e}")
    since = data["candles"][0]["time"]
    marks = []
    try:
        rows = _closed_trades(2000) + _state().get_open_positions()
    except Exception:
        rows = []
    for t in rows:
        if t.get("symbol") != symbol:
            continue
        for kind, ts, px in (("entry", t.get("opened_at"), t.get("entry_price")),
                             ("exit", t.get("closed_at"), t.get("exit_price"))):
            dt = None
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() if ts else None
            except Exception:
                pass
            if dt and px and dt >= since:
                marks.append({"time": int(dt), "kind": kind, "side": t.get("side"), "price": px,
                              "pnl": t.get("pnl")})
    data["markers"] = marks
    return data
