"""
Enhanced Dashboard + control API with WebSocket live streaming.

Endpoints:
  GET  /health                -> health check for watchdog
  GET  /api/status            -> current risk state, positions, events, regime
  GET  /api/trades            -> recent closed trades
  GET  /api/trades/history    -> full trade history with filtering
  GET  /api/profit            -> all-time stats
  GET  /api/equity            -> equity curve data
  GET  /api/symbol-stats      -> per-symbol performance
  GET  /api/hourly-logs       -> hourly report history
  GET  /api/account           -> stake + trading balance + ROI%
  GET  /api/strategy-stats    -> per-strategy accuracy/win-rate
  GET  /api/exchange-health   -> per-exchange API failure rate
  GET  /api/slippage          -> signal-vs-fill execution quality
  GET  /api/long-term/summary -> long-term buy/sell candidates + movers
  GET  /api/open-positions    -> current open positions
  POST /api/kill-switch       -> emergency halt (auth required)
  POST /api/resume            -> resume trading (auth required)
  POST /api/terminal          -> execute VPS command (auth required)
  WS   /ws                    -> real-time data streaming (trades, prices, balance)
  GET  /                      -> dashboard HTML UI
"""
import json
import os
import sys
import asyncio
import subprocess
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware

sys.path.append(str(Path(__file__).parent.parent))
from core.state_manager import StateManager, SNAPSHOT_PATH
from alerts.notifier import Notifier, EVENT_LOG, TRADE_LOG
from reporting.hourly_report import read_hourly_log
from core.structured_logger import slippage_tracker, api_failure_tracker, strategy_perf_tracker

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
with open(_CONFIG_PATH) as _f:
    CONFIG = yaml.safe_load(_f)
_env_stake = os.environ.get("STAKE_AMOUNT")
if _env_stake is not None:
    try:
        CONFIG["account"]["stake_amount"] = float(_env_stake)
    except ValueError:
        pass

app = FastAPI(title="Wamucheha Trading Bot Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET_KEY", "change_me")

_start_time = datetime.now(timezone.utc)
_state_manager = None

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._last_price_cache = {}
        self._trade_buffer = []
        self._max_buffer = 50

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        dead = []
        for conn in self.active_connections:
            try:
                await conn.send_json(data)
            except Exception:
                dead.append(conn)
        for d in dead:
            self.active_connections.remove(d)

    def buffer_trade(self, trade_data: dict):
        self._trade_buffer.append(trade_data)
        if len(self._trade_buffer) > self._max_buffer:
            self._trade_buffer = self._trade_buffer[-self._max_buffer:]

ws_manager = ConnectionManager()


def _get_state():
    global _state_manager
    if _state_manager is None:
        _state_manager = StateManager(stake_amount=0)
    return _state_manager


def _load_snapshot():
    if SNAPSHOT_PATH.exists():
        return json.loads(SNAPSHOT_PATH.read_text())
    return {}


def _load_recent_events(n=50):
    if not EVENT_LOG.exists():
        return []
    try:
        lines = EVENT_LOG.read_text().strip().splitlines()[-n:]
        return [json.loads(l) for l in reversed(lines)]
    except Exception:
        return []


def _load_trade_log(n=50):
    if not TRADE_LOG.exists():
        return []
    try:
        lines = TRADE_LOG.read_text().strip().splitlines()[-n:]
        return [json.loads(l) for l in reversed(lines)]
    except Exception:
        return []


def _load_market_regime():
    cache_dir = Path(__file__).parent.parent / "data" / "intel_cache"
    cache_file = cache_dir / "regime.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    return {}


# ---------- Health & Status ----------

@app.get("/health")
def health():
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
        "market_regime": _load_market_regime(),
    }


# ---------- Trades ----------

@app.get("/api/trades")
def trades(n: int = 20):
    state = _get_state()
    return {"trades": state.get_recent_trades(n)}


@app.get("/api/trades/history")
def trade_history(symbol: str | None = None, hours: int | None = None, n: int = 50):
    state = _get_state()
    if hours:
        trades = state.get_trades_by_timeframe(hours)
    elif symbol:
        trades = state.get_trades_by_symbol(symbol.upper(), n)
    else:
        trades = state.get_recent_trades(n)
    return {"trades": trades}


@app.get("/api/trade-log")
def trade_log(n: int = 50):
    return {"log": _load_trade_log(n)}


# ---------- Stats ----------

@app.get("/api/profit")
def profit():
    state = _get_state()
    return state.get_all_time_stats()


@app.get("/api/equity")
def equity(n: int = 200):
    state = _get_state()
    return {"curve": state.get_equity_curve(n)}


@app.get("/api/symbol-stats")
def symbol_stats():
    state = _get_state()
    return state.get_symbol_stats()


@app.get("/api/hourly-logs")
def hourly_logs(n: int = 24):
    return {"hourly_logs": read_hourly_log(n)}


@app.get("/api/account")
def account():
    """Full account picture: protected stake (never traded) + trading
    balance (at risk) + ROI, so 'my balance' means something complete
    rather than just the at-risk figure."""
    state = _get_state()
    risk_state = state.get_risk_state()
    stats = state.get_all_time_stats()

    stake_amount = CONFIG.get("account", {}).get("stake_amount", 0.0)
    trading_balance = risk_state.get("trading_balance", 0.0)
    net_pnl = stats.get("net_pnl", 0.0)
    # trading_balance = initial_funding + net_pnl, so initial_funding is
    # recoverable without a separate persisted field.
    initial_funding = trading_balance - net_pnl
    roi_pct = (net_pnl / initial_funding * 100) if initial_funding > 0 else 0.0

    return {
        "stake_amount": stake_amount,
        "trading_balance": trading_balance,
        "peak_balance": risk_state.get("peak_balance", 0.0),
        "total_account_value": stake_amount + trading_balance,
        "initial_funding": round(initial_funding, 2),
        "net_pnl": net_pnl,
        "roi_pct": round(roi_pct, 2),
        "daily_pnl": risk_state.get("daily_pnl", 0.0),
        "consecutive_losses": risk_state.get("consecutive_losses", 0),
        "trading_halted": bool(risk_state.get("trading_halted", 0)),
        "halt_reason": risk_state.get("halt_reason"),
    }


@app.get("/api/strategy-stats")
def strategy_stats():
    """Per-strategy accuracy: win rate, trade count, and PnL for each of the
    20 sub-strategies, computed from actual closed-trade outcomes (see
    core/structured_logger.StrategyPerformanceTracker) — this is the
    'which strategies are actually right' view, distinct from overall
    win rate."""
    return {
        "strategies": strategy_perf_tracker.get_strategy_stats(),
        "underperforming": strategy_perf_tracker.get_underperforming(),
    }


@app.get("/api/exchange-health")
def exchange_health():
    """Per-exchange API failure rate over the last 5 minutes — a more
    specific health signal than the overall /health check."""
    return api_failure_tracker.get_health()


@app.get("/api/slippage")
def slippage():
    """Execution quality: signal price vs actual fill price."""
    return slippage_tracker.get_stats()


# ---------- Long-Term Investing ----------

@app.get("/api/long-term/summary")
def long_term_summary():
    cache_file = Path(__file__).parent.parent / "data" / "intel_cache" / "long_term_dashboard.json"
    if not cache_file.exists():
        return {"buy_candidates": [], "sell_candidates": [], "movers": {}, "updated_at": None,
                "note": "No data yet — long_term/scheduler.py hasn't run a refresh."}
    try:
        return json.loads(cache_file.read_text())
    except Exception:
        return {"buy_candidates": [], "sell_candidates": [], "movers": {}, "updated_at": None}


@app.get("/api/open-positions")
def open_positions():
    state = _get_state()
    return {"positions": state.get_open_positions()}


# ---------- Control ----------

@app.post("/api/kill-switch")
def kill_switch(x_dashboard_key: str = Header(None)):
    if x_dashboard_key != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    state = _get_state()
    state.update_risk_state(trading_halted=1, halt_reason="Manual kill switch via dashboard")
    return {"status": "halted"}


@app.post("/api/resume")
def resume(x_dashboard_key: str = Header(None)):
    if x_dashboard_key != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    state = _get_state()
    state.update_risk_state(trading_halted=0, halt_reason=None)
    return {"status": "resumed"}


# ---------- Terminal ----------

@app.post("/api/terminal")
def terminal_exec(body: dict, x_dashboard_key: str = Header(None)):
    if x_dashboard_key != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    command = body.get("command", "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="No command provided")

    # Block dangerous commands
    blocked = ["rm -rf", "mkfs", "dd if=", "> /dev/", "shutdown", "reboot", "halt",
                "init 0", "init 6", "format", ":(){ :|:& };:"]
    for b in blocked:
        if b in command.lower():
            raise HTTPException(status_code=400, detail=f"Blocked dangerous command: {b}")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30,
            cwd=str(Path(__file__).parent.parent),
        )
        return {
            "stdout": result.stdout[-5000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out after 30s", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


# ---------- WebSocket ----------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # Send periodic updates
            snapshot = _load_snapshot()
            stats = _get_state().get_all_time_stats()
            positions = _get_state().get_open_positions()

            await websocket.send_json({
                "type": "update",
                "snapshot": snapshot,
                "stats": stats,
                "positions": positions,
                "regime": _load_market_regime(),
                "ts": datetime.now(timezone.utc).isoformat(),
            })

            await asyncio.sleep(3)

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# ---------- Dashboard UI ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
