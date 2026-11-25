"""
Dry test: a one-shot, order-free self-test of the whole decision chain.

It answers "if I let this run, would it work?" without risking anything:
  * is each broker/exchange executor connected, and is it simulated?
  * does the data feed return recent candles for each market?
  * does the strategy produce a decision (buy/sell/none) from that data?
Nothing here can place an order: it only calls feeds and the signal function.
The result is written atomically to <CONTROL_DIR>/dry_test.json for the dashboard.
"""
import json
import time
from datetime import datetime, timezone

from core import control_channel


def run_dry_test(executors: dict, feed_router, markets: list, signal_fn, mode_label: str,
                 timeframe: str = "15m") -> dict:
    checks = []

    def add(name, ok, detail, level=None):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail),
                       "level": level or ("ok" if ok else "bad")})

    add("Mode", True, mode_label)
    if not executors:
        add("Executors", False, "no broker/exchange executor is connected, so nothing can trade")
    for name, ex in executors.items():
        simulated = getattr(ex, "dry_run", None)
        add(f"Executor: {name}", True,
            "connected, orders are SIMULATED" if simulated else
            "connected, orders are REAL" if simulated is False else "connected",
            level="ok" if simulated else "warn")

    for venue, symbol in markets:
        label = f"{venue}:{symbol}"
        if venue not in executors:
            add(f"Data {label}", False, f"no executor for '{venue}' (connection failed or venue disabled)")
            continue
        try:
            df = feed_router.get_ohlcv(symbol, timeframe, 200)
        except Exception as e:
            add(f"Data {label}", False, f"feed error: {e}")
            continue
        if df is None or len(df) < 50:
            add(f"Data {label}", False, f"only {0 if df is None else len(df)} candles (need 50+)")
            continue
        add(f"Data {label}", True, f"{len(df)} candles, last close {float(df['close'].iloc[-1]):g}")
        try:
            sig = signal_fn(symbol)
        except Exception as e:
            add(f"Signal {label}", False, f"strategy error: {type(e).__name__}: {e}")
            continue
        if sig:
            add(f"Signal {label}", True,
                f"{sig.get('side')} score {sig.get('score', 0):.2f} "
                f"({', '.join(sig.get('strategies', [])[:3]) or 'n/a'}); would size {sig.get('amount')}")
        else:
            add(f"Signal {label}", True, "no trade right now (the strategy is working, nothing qualifies)")

    failed = [c for c in checks if not c["ok"]]
    result = {"ts": datetime.now(timezone.utc).isoformat(), "mode": mode_label,
              "passed": not failed, "checks": checks,
              "summary": "All checks passed" if not failed else f"{len(failed)} check(s) failed"}
    try:
        d = control_channel.CONTROL_DIR
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / ".dry_test.tmp"
        tmp.write_text(json.dumps(result))
        tmp.replace(d / "dry_test.json")
    except OSError:
        pass
    return result


def read_dry_test() -> dict:
    try:
        return json.loads((control_channel.CONTROL_DIR / "dry_test.json").read_text())
    except Exception:
        return {}
