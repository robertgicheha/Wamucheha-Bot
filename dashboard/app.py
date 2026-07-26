"""
Dashboard + control API.

/health          -> polled by the local watchdog to detect a dead/frozen bot
/api/status      -> current risk state, open positions, recent events
/api/kill-switch -> POST, manually halt all trading immediately (auth required)
/api/resume      -> POST, manually resume after review (auth required)
/                -> simple live-updating HTML view
"""
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request

import sys
sys.path.append(str(Path(__file__).parent.parent))
from core.state_manager import StateManager, SNAPSHOT_PATH
from core.risk_manager import RiskManager
from alerts.notifier import Notifier, EVENT_LOG
from reporting.hourly_report import read_hourly_log

app = FastAPI(title="Trading Bot Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET_KEY", "change_me")

_start_time = datetime.now(timezone.utc)


def _load_snapshot():
    if SNAPSHOT_PATH.exists():
        return json.loads(SNAPSHOT_PATH.read_text())
    return {}


def _load_recent_events(n=25):
    if not EVENT_LOG.exists():
        return []
    lines = EVENT_LOG.read_text().strip().splitlines()[-n:]
    return [json.loads(l) for l in reversed(lines)]


@app.get("/health")
def health():
    """Watchdog on the local machine polls this. If it stops responding or the
    'last_trade_engine_tick' is stale, the watchdog fires an alert independently
    of anything the VPS itself is able to report (since it's the VPS that's failing)."""
    snapshot = _load_snapshot()
    return {
        "status": "ok",
        "uptime_seconds": (datetime.now(timezone.utc) - _start_time).total_seconds(),
        "trading_halted": bool(snapshot.get("trading_halted", 0)),
        "last_state_update": snapshot.get("updated_at"),
    }


@app.get("/api/status")
def status():
    return {
        "risk_state": _load_snapshot(),
        "recent_events": _load_recent_events(),
    }


@app.get("/api/trades")
def trades(n: int = 20):
    state = StateManager(stake_amount=0)
    return {"trades": state.get_recent_trades(n)}


@app.get("/api/profit")
def profit():
    state = StateManager(stake_amount=0)
    return state.get_all_time_stats()


@app.get("/api/hourly-logs")
def hourly_logs(n: int = 24):
    return {"hourly_logs": read_hourly_log(n)}


@app.post("/api/kill-switch")
def kill_switch(x_dashboard_key: str = Header(None)):
    if x_dashboard_key != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    state = StateManager(stake_amount=0)
    state.update_risk_state(trading_halted=1, halt_reason="Manual kill switch via dashboard")
    return {"status": "halted"}


@app.post("/api/resume")
def resume(x_dashboard_key: str = Header(None)):
    if x_dashboard_key != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    state = StateManager(stake_amount=0)
    state.update_risk_state(trading_halted=0, halt_reason=None, consecutive_losses=0)
    return {"status": "resumed"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})