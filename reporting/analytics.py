"""
Trade analytics and statistical projection for the admin dashboard.

Pure functions over closed-trade dicts (StateManager TradeRow.to_dict() shape:
pnl, pnl_pct, fees, symbol, regime, strategies, opened_at, closed_at), so they
are testable without a database and reusable from scripts.

"Predictive" here means a bootstrap Monte Carlo of the bot's OWN realised trade
results: it answers "if the next N trades look like the last ones, what range
of outcomes is plausible, and how likely is a drawdown past my limit?". It is
NOT a forecast of market prices, and it says so: with few trades the output is
flagged unreliable, because resampling 12 trades tells you about those 12.
"""
import json
from collections import defaultdict
from datetime import datetime

import numpy as np

MIN_TRADES_FOR_PROJECTION = 30


def _f(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _strategies(t) -> list:
    s = t.get("strategies")
    if isinstance(s, list):
        return s
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _max_drawdown(cum: np.ndarray, base: float = 0.0):
    """Max peak-to-trough drop of an equity curve (absolute, and % of peak equity)."""
    if len(cum) == 0:
        return 0.0, 0.0
    eq = base + cum
    peak = np.maximum.accumulate(np.concatenate([[base], eq]))[1:]
    dd = eq - peak
    abs_dd = float(dd.min())
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(peak > 0, dd / peak, 0.0)
    return abs_dd, float(pct.min() * 100)


def _streaks(pnls):
    best_w = best_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    cur = 0
    for p in reversed(pnls):
        sign = 1 if p > 0 else -1 if p < 0 else 0
        if sign == 0 or (cur and (cur > 0) != (sign > 0)):
            break
        cur += sign
    return {"max_win_streak": best_w, "max_loss_streak": best_l, "current_streak": cur}


def compute_metrics(trades: list, starting_balance: float = 0.0) -> dict:
    """Headline performance numbers from closed trades (any order)."""
    closed = [t for t in trades if t.get("pnl") is not None]
    closed.sort(key=lambda t: str(t.get("closed_at") or t.get("opened_at") or ""))
    pnls = np.array([_f(t["pnl"]) for t in closed])
    n = len(pnls)
    if n == 0:
        return {"trades": 0}
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    cum = np.cumsum(pnls)
    dd_abs, dd_pct = _max_drawdown(cum, starting_balance)
    holds = []
    for t in closed:
        a, b = _parse_ts(t.get("opened_at")), _parse_ts(t.get("closed_at"))
        if a and b:
            holds.append((b - a).total_seconds() / 3600)
    std = float(pnls.std(ddof=1)) if n > 1 else 0.0
    return {
        "trades": n,
        "wins": int(len(wins)), "losses": int(len(losses)),
        "breakeven": int(n - len(wins) - len(losses)),
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "loss_rate_pct": round(len(losses) / n * 100, 1),
        "gross_profit": round(gross_win, 4), "gross_loss": round(-gross_loss, 4),
        # Share of gross winnings you kept after losses and fees: 100% = no losses at all.
        "efficiency_pct": round(float(pnls.sum()) / gross_win * 100, 1) if gross_win > 0 else None,
        "total_pnl": round(float(pnls.sum()), 4),
        "total_fees": round(sum(_f(t.get("fees")) for t in closed), 4),
        "avg_win": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "best_trade": round(float(pnls.max()), 4),
        "worst_trade": round(float(pnls.min()), 4),
        "expectancy": round(float(pnls.mean()), 4),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "payoff_ratio": round(float(wins.mean() / -losses.mean()), 2)
                        if len(wins) and len(losses) else None,
        "sharpe_per_trade": round(float(pnls.mean() / std), 3) if std > 0 else None,
        "max_drawdown": round(dd_abs, 4),
        "max_drawdown_pct": round(dd_pct, 2),
        "avg_hold_hours": round(float(np.mean(holds)), 2) if holds else None,
        **_streaks(list(pnls)),
    }


def _group(trades, keyfn) -> list:
    buckets = defaultdict(list)
    for t in trades:
        if t.get("pnl") is None:
            continue
        for k in keyfn(t):
            buckets[k].append(_f(t["pnl"]))
    rows = []
    for k, v in buckets.items():
        a = np.array(v)
        rows.append({"key": k, "trades": len(v), "pnl": round(float(a.sum()), 4),
                     "win_rate_pct": round(float((a > 0).mean() * 100), 1),
                     "avg_pnl": round(float(a.mean()), 4)})
    return sorted(rows, key=lambda r: r["pnl"], reverse=True)


def breakdowns(trades: list) -> dict:
    """Where the money is made/lost: by symbol, strategy, regime, hour (UTC), weekday."""
    def hour(t):
        d = _parse_ts(t.get("closed_at"))
        return [f"{d.hour:02d}:00"] if d else []

    def weekday(t):
        d = _parse_ts(t.get("closed_at"))
        return [d.strftime("%a")] if d else []

    return {
        "by_symbol": _group(trades, lambda t: [t.get("symbol") or "?"]),
        "by_strategy": _group(trades, lambda t: _strategies(t) or ["(none)"]),
        "by_regime": _group(trades, lambda t: [t.get("regime") or "unknown"]),
        "by_hour": sorted(_group(trades, hour), key=lambda r: r["key"]),
        "by_weekday": _group(trades, weekday),
        "by_exchange": _group(trades, lambda t: [t.get("exchange") or "?"]),
    }


def project_equity(trades: list, balance: float, horizon_trades: int = 50,
                   simulations: int = 2000, max_drawdown_limit_pct: float = 5.0,
                   seed: int = 7) -> dict:
    """Bootstrap Monte Carlo of the next `horizon_trades` trades.

    Resamples realised trade P&Ls with replacement. Returns percentile bands of
    equity per step, P(final < start), and P(drawdown beyond the risk limit at
    any point). Deterministic for a given seed so the dashboard doesn't flicker.
    """
    pnls = np.array([_f(t["pnl"]) for t in trades if t.get("pnl") is not None])
    n = len(pnls)
    result = {"sample_trades": n, "horizon_trades": horizon_trades,
              "reliable": n >= MIN_TRADES_FOR_PROJECTION,
              "note": ("Resampled from your own past trades; not a market forecast. "
                       "Past distribution may not repeat."
                       if n >= MIN_TRADES_FOR_PROJECTION else
                       f"Only {n} closed trades; need >= {MIN_TRADES_FOR_PROJECTION} "
                       "before this projection means anything.")}
    if n < 2 or balance <= 0:
        result["bands"] = []
        return result

    rng = np.random.default_rng(seed)
    draws = rng.choice(pnls, size=(simulations, horizon_trades), replace=True)
    paths = balance + np.cumsum(draws, axis=1)
    peaks = np.maximum.accumulate(np.concatenate(
        [np.full((simulations, 1), balance), paths], axis=1), axis=1)[:, 1:]
    dd_pct = np.where(peaks > 0, (paths - peaks) / peaks * 100, 0.0).min(axis=1)

    step = max(1, horizon_trades // 25)
    idx = list(range(0, horizon_trades, step))
    if idx[-1] != horizon_trades - 1:
        idx.append(horizon_trades - 1)
    q = np.percentile(paths[:, idx], [5, 25, 50, 75, 95], axis=0)
    # Expectancy uncertainty: bootstrap the mean itself.
    means = rng.choice(pnls, size=(simulations, n), replace=True).mean(axis=1)
    result.update({
        "start_balance": round(balance, 4),
        "bands": [{"trade": i + 1, "p5": round(float(q[0][j]), 4), "p25": round(float(q[1][j]), 4),
                   "p50": round(float(q[2][j]), 4), "p75": round(float(q[3][j]), 4),
                   "p95": round(float(q[4][j]), 4)} for j, i in enumerate(idx)],
        "prob_loss_pct": round(float((paths[:, -1] < balance).mean() * 100), 1),
        "prob_breach_drawdown_pct": round(float((dd_pct <= -abs(max_drawdown_limit_pct)).mean() * 100), 1),
        "drawdown_limit_pct": max_drawdown_limit_pct,
        "median_final": round(float(np.median(paths[:, -1])), 4),
        "expectancy_ci95": [round(float(np.percentile(means, 2.5)), 4),
                            round(float(np.percentile(means, 97.5)), 4)],
        "edge_is_positive_with_95pct_confidence": bool(np.percentile(means, 2.5) > 0),
    })
    return result


def equity_trend(trades: list, window: int = 30) -> dict:
    """Slope of cumulative P&L over the last `window` trades (P&L per trade, drift)."""
    closed = sorted([t for t in trades if t.get("pnl") is not None],
                    key=lambda t: str(t.get("closed_at") or ""))[-window:]
    if len(closed) < 5:
        return {"window": len(closed), "slope_per_trade": None, "direction": "insufficient data"}
    y = np.cumsum([_f(t["pnl"]) for t in closed])
    slope = float(np.polyfit(np.arange(len(y)), y, 1)[0])
    tol = abs(float(np.std(np.diff(y)))) * 0.1
    return {"window": len(closed), "slope_per_trade": round(slope, 4),
            "direction": "improving" if slope > tol else "deteriorating" if slope < -tol else "flat"}
