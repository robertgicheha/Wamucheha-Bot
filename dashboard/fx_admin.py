"""
Admin dashboard for the FX trend bot.

  python -m dashboard.fx_admin                      # http://127.0.0.1:8090
  FX_ADMIN_TOKEN=<long random string> is REQUIRED; every /api call needs it.

Read:   overview, positions, trades, signals, analysis, predictive, backtest,
        events, logs, config
Control: start (dry-run / orders), stop (graceful), restart, flatten-and-stop,
         clear kill file

Security: binds to 127.0.0.1 by default (reach it over an SSH tunnel or put it
behind your reverse proxy with TLS). Token compare is constant-time; repeated
bad tokens lock that client out for a minute. Order-enabling and flatten
actions need a typed confirmation. The dashboard can never switch the bot to a
LIVE account: that requires CLI flags on the bot itself.
"""
from __future__ import annotations

import bisect
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from core.fx_trend_analytics import performance, project_equity
from core.fx_trend_journal import Journal
from core.fx_trend_supervisor import BotSupervisor

ROOT = Path(__file__).resolve().parent.parent
LOG_FILES = {"bot": "fx_trend.log", "errors": "fx_trend_errors.log", "console": "fx_trend_console.log"}
MAX_FAILS, LOCKOUT_S = 5, 60


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _age(iso: str | None) -> float | None:
    try:
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())
    except Exception:
        return None


def tail_lines(path: Path, n: int, needle: str = "", level: str = "") -> list[str]:
    """Last n matching lines without reading a huge file into memory."""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 1_500_000))
        text = f.read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    if needle:
        lines = [ln for ln in lines if needle.lower() in ln.lower()]
    if level:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        keep = set(order[order.index(level):]) if level in order else set(order)
        lines = [ln for ln in lines if any(f" {lv} " in ln for lv in keep) or not ln[:4].isdigit()]
    return lines[-n:]


def trade_returns(trades: list[dict], cycles: list[dict]) -> list[float]:
    """Per-trade return as a fraction of equity at the time the trade closed."""
    bars = [c["bar"] or "" for c in cycles]
    out = []
    for t in sorted(trades, key=lambda t: t["close_time"] or ""):
        net = t["profit"] + (t.get("commission") or 0) + (t.get("swap") or 0)
        i = bisect.bisect_right(bars, (t["close_time"] or "")[:10]) - 1
        eq = cycles[i]["equity"] if 0 <= i < len(cycles) else (cycles[0]["equity"] if cycles else 0)
        if eq:
            out.append(net / eq)
    return out


class ControlBody(BaseModel):
    action: str
    mode: str = "dry_run"        # dry_run | orders
    confirm: str = ""


def create_app(root: Path | str = ROOT, token: str | None = None) -> FastAPI:
    root = Path(root)
    token = token if token is not None else os.environ.get("FX_ADMIN_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("Set FX_ADMIN_TOKEN to a random string of at least 16 characters.")

    app = FastAPI(title="FX Trend Bot Admin", docs_url=None, redoc_url=None, openapi_url=None)
    journal = Journal(root / "data" / "fx_trend.db")
    sup = BotSupervisor(root)
    fails: dict[str, list[float]] = {}

    def auth(request: Request):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < LOCKOUT_S]
        fails[ip] = recent
        if len(recent) >= MAX_FAILS:
            raise HTTPException(429, "Too many failed attempts, try again in a minute")
        supplied = request.headers.get("x-admin-token") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            recent.append(now)
            raise HTTPException(401, "Invalid admin token")

    protected = [Depends(auth)]

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "templates" / "fx_admin.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        return {"ok": True}

    # ------------------------------------------------------------------ reads
    @app.get("/api/overview", dependencies=protected)
    def overview():
        snap = _read_json(root / "data" / "fx_trend_snapshot.json", {}) or {}
        state = _read_json(root / "data" / "fx_trend_state.json", {}) or {}
        cycles, trades = journal.equity_series(5000), journal.trades(5000)
        perf = performance(trades, cycles)
        perf.pop("drawdown_series", None)
        last = cycles[-1] if cycles else {}
        errors = [e for e in journal.events(300, "ERROR")
                  if (_age(e["ts"]) or 1e9) < 86400]
        return {
            "bot": sup.status(), "snapshot": snap, "snapshot_age_s": _age(snap.get("ts")),
            "state": state, "last_cycle": last, "performance": perf,
            "errors_24h": len(errors),
            "equity": [{"t": c["bar"] or c["ts"][:10], "v": c["equity"]} for c in cycles[-1500:]],
            "events": journal.events(15),
        }

    @app.get("/api/positions", dependencies=protected)
    def positions():
        snap = _read_json(root / "data" / "fx_trend_snapshot.json", {}) or {}
        return {"ts": snap.get("ts"), "age_s": _age(snap.get("ts")), "mode": snap.get("mode"),
                "positions": snap.get("positions", []), "floating_pnl": snap.get("floating_pnl", 0)}

    @app.get("/api/trades", dependencies=protected)
    def trades(limit: int = Query(500, le=5000)):
        return {"trades": journal.trades(limit)}

    @app.get("/api/signals", dependencies=protected)
    def signals(pair: str | None = None):
        if pair:
            return {"pair": pair, "history": journal.signal_history(pair)}
        return {"signals": journal.latest_signals()}

    @app.get("/api/analysis", dependencies=protected)
    def analysis():
        return performance(journal.trades(5000), journal.equity_series(5000))

    @app.get("/api/predictive", dependencies=protected)
    def predictive():
        sigs = journal.latest_signals()
        cycles, trades = journal.equity_series(5000), journal.trades(5000)
        rets, source = trade_returns(trades, cycles), "live trades"
        if len(rets) < 10:
            bt = _read_json(root / "data" / "fx_trend_backtest.json", {}) or {}
            if bt.get("trade_returns"):
                rets, source = bt["trade_returns"], f"backtest ({bt.get('source')}), too few live trades yet"
        proj = project_equity(rets) if rets else {"ok": False, "reason": "no trades or backtest yet"}
        proj["source"] = source
        snap = _read_json(root / "data" / "fx_trend_snapshot.json", {}) or {}
        net: dict[str, int] = {}
        for p in snap.get("positions", []):
            pair = p["pair"][:6]
            net[pair[:3]] = net.get(pair[:3], 0) + p["side"]
            net[pair[3:6]] = net.get(pair[3:6], 0) - p["side"]
        n = len(sigs) or 1
        regime = {
            "long": sum(s["side"] > 0 for s in sigs), "short": sum(s["side"] < 0 for s in sigs),
            "flat": sum(s["side"] == 0 for s in sigs),
            "trend_breadth_pct": round(100 * sum(s["side"] != 0 for s in sigs) / n),
            "avg_abs_strength_atr": round(sum(abs(s["strength"]) for s in sigs) / n, 2),
        }
        return {"flips": sigs, "projection": proj, "regime": regime, "currency_net": net,
                "disclaimer": "Statistical projections under a random-walk / bootstrapped-trades "
                              "assumption. They describe uncertainty, they do not forecast the market."}

    @app.get("/api/backtest", dependencies=protected)
    def backtest():
        return _read_json(root / "data" / "fx_trend_backtest.json", {}) or {}

    @app.get("/api/events", dependencies=protected)
    def events(limit: int = Query(200, le=2000), level: str | None = None):
        return {"events": journal.events(limit, level)}

    @app.get("/api/logs", dependencies=protected)
    def logs(file: str = "errors", lines: int = Query(200, le=2000), q: str = "", level: str = ""):
        if file not in LOG_FILES:
            raise HTTPException(400, f"file must be one of {list(LOG_FILES)}")
        path = root / "data" / "logs" / LOG_FILES[file]
        return {"file": file, "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
                "lines": tail_lines(path, lines, q, level.upper())}

    @app.get("/api/config", dependencies=protected)
    def config():
        path = root / "config" / "fx_trend.yaml"
        text = path.read_text() if path.exists() else ""
        return {"yaml": text, "parsed": yaml.safe_load(text) if text else {}}

    # ------------------------------------------------------------------ control
    @app.post("/api/control", dependencies=protected)
    def control(body: ControlBody, request: Request):
        a, orders = body.action, body.mode == "orders"
        if a in ("start", "restart") and orders and body.confirm != "SEND ORDERS":
            raise HTTPException(400, 'Type "SEND ORDERS" to confirm enabling real order placement')
        if a == "flatten" and body.confirm != "FLATTEN":
            raise HTTPException(400, 'Type "FLATTEN" to confirm closing all of this bot\'s positions')
        actions = {
            "start": lambda: sup.start(orders), "stop": sup.stop, "restart": lambda: sup.restart(orders),
            "flatten": sup.flatten_and_stop, "clear_kill": sup.clear_kill,
        }
        if a not in actions:
            raise HTTPException(400, f"unknown action {a!r}")
        result = actions[a]()
        journal.add_event("control", f"dashboard action {a} ({body.mode}) from "
                                     f"{request.client.host if request.client else '?'}: {result['message']}",
                          level="INFO" if result.get("ok") else "WARNING")
        return JSONResponse({**result, "status": sup.status()}, status_code=200 if result.get("ok") else 409)

    return app


def main():
    import argparse
    import uvicorn
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FX_ADMIN_PORT", "8090")))
    args = ap.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
