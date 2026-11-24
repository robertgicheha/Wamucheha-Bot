"""
Pure analytics for the FX trend bot: performance analysis of what happened, and
"predictive" analysis of what could happen.

IMPORTANT: the predictive numbers are statistical projections under stated
assumptions (random-walk price, bootstrapped past trade results). They are not
forecasts of what the market will do, and the dashboard labels them as such.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

import numpy as np

SQRT2 = math.sqrt(2.0)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


# --------------------------------------------------------------------------
# Predictive: how close is each pair to changing state?
# --------------------------------------------------------------------------
def p_touch(distance: float, sigma_daily: float, days: int) -> float:
    """P(price touches a level `distance` away within `days`), driftless random
    walk (reflection principle): 2 * (1 - Phi(d / (sigma*sqrt(days))))."""
    if sigma_daily <= 0 or days <= 0:
        return 0.0
    return float(min(1.0, max(0.0, 2.0 * (1.0 - _phi(abs(distance) / (sigma_daily * math.sqrt(days)))))))


def signal_levels(close_series, sma: float, mom_len: int) -> tuple[float, float]:
    """Price bands that define the signal: above up_level = long, below
    dn_level = short, between = flat. Uses the price mom_len bars ago as the
    momentum reference (it drifts forward daily, so this is an approximation)."""
    ref = float(close_series.iloc[-mom_len]) if len(close_series) >= mom_len else float(close_series.iloc[0])
    return max(sma, ref), min(sma, ref)


def flip_analysis(side: int, close: float, atr: float, up_level: float, dn_level: float) -> dict:
    """Probability the pair leaves its current state within 5 / 20 days."""
    sigma = 1.25 * atr  # ATR ~ 0.8 x daily sigma for a random walk
    out = {}
    for h in (5, 20):
        if side > 0:
            p = p_touch(close - up_level, sigma, h)
        elif side < 0:
            p = p_touch(dn_level - close, sigma, h)
        else:
            p = min(1.0, p_touch(up_level - close, sigma, h) + p_touch(close - dn_level, sigma, h))
        out[f"p_flip_{h}d"] = round(p, 4)
    return out


def strength(close: float, sma: float, atr: float) -> float:
    """Distance from the SMA in ATRs (signed). Bigger magnitude = firmer trend."""
    return round((close - sma) / atr, 3) if atr else 0.0


# --------------------------------------------------------------------------
# Predictive: equity projection by bootstrapping trade results
# --------------------------------------------------------------------------
def project_equity(trade_returns: list[float], horizon_trades: int = 60, n_sims: int = 2000,
                   seed: int = 11) -> dict:
    """Fan chart of cumulative return over the next `horizon_trades` trades,
    resampling past per-trade returns (fraction of equity) with replacement."""
    r = np.asarray(trade_returns, float)
    if len(r) < 10:
        return {"ok": False, "reason": f"need >=10 closed trades, have {len(r)}"}
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(n_sims, horizon_trades), replace=True)
    paths = np.cumprod(1.0 + draws, axis=1)
    peaks = np.maximum.accumulate(np.concatenate([np.ones((n_sims, 1)), paths], axis=1), axis=1)[:, 1:]
    dd = (paths / peaks - 1.0).min(axis=1)
    final = paths[:, -1] - 1.0
    q = lambda a, p: np.percentile(a, p, axis=0)  # noqa: E731
    return {
        "ok": True, "n_trades_used": int(len(r)), "horizon_trades": horizon_trades,
        "p5": [round(float(v), 5) for v in q(paths, 5) - 1],
        "p50": [round(float(v), 5) for v in q(paths, 50) - 1],
        "p95": [round(float(v), 5) for v in q(paths, 95) - 1],
        "prob_loss": round(float((final < 0).mean()), 3),
        "prob_dd_gt_5pct": round(float((dd < -0.05).mean()), 3),
        "prob_dd_gt_10pct": round(float((dd < -0.10).mean()), 3),
        "median_return": round(float(np.median(final)), 4),
        "expectancy_per_trade": round(float(r.mean()), 5),
    }


# --------------------------------------------------------------------------
# Performance analysis of realised results
# --------------------------------------------------------------------------
def _dt(s: str | None):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def performance(trades: list[dict], equity: list[dict]) -> dict:
    """trades: journal rows (profit/commission/swap already in account currency)."""
    out: dict = {"n_trades": len(trades)}
    net = [t["profit"] + (t.get("commission") or 0) + (t.get("swap") or 0) for t in trades]
    if trades:
        arr = np.array(net)
        wins, losses = arr[arr > 0], arr[arr < 0]
        out.update(
            net_pnl=round(float(arr.sum()), 2),
            win_rate=round(float((arr > 0).mean()), 3),
            avg_win=round(float(wins.mean()), 2) if len(wins) else 0.0,
            avg_loss=round(float(losses.mean()), 2) if len(losses) else 0.0,
            profit_factor=round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
            expectancy=round(float(arr.mean()), 2),
            best=round(float(arr.max()), 2), worst=round(float(arr.min()), 2),
        )
        # streaks (chronological)
        chron = [n for _, n in sorted(zip([t["close_time"] or "" for t in trades], net))]
        cur = best_w = best_l = 0
        for n in chron:
            cur = (cur + 1 if cur > 0 else 1) if n > 0 else (cur - 1 if cur < 0 else -1) if n < 0 else 0
            best_w, best_l = max(best_w, cur), min(best_l, cur)
        out.update(max_win_streak=best_w, max_loss_streak=-best_l)
        hold = [((_dt(t["close_time"]) - _dt(t["open_time"])).total_seconds() / 86400)
                for t in trades if _dt(t["close_time"]) and _dt(t["open_time"])]
        out["avg_hold_days"] = round(float(np.mean(hold)), 1) if hold else None

        by_pair, by_month, by_side = defaultdict(lambda: [0, 0.0, 0]), defaultdict(float), defaultdict(float)
        for t, n in zip(trades, net):
            bp = by_pair[t["pair"]]
            bp[0] += 1; bp[1] += n; bp[2] += n > 0
            by_month[(t["close_time"] or "")[:7]] += n
            by_side["long" if t["side"] > 0 else "short"] += n
        out["by_pair"] = [{"pair": k, "trades": v[0], "net": round(v[1], 2),
                           "win_rate": round(v[2] / v[0], 3)} for k, v in sorted(by_pair.items())]
        out["by_month"] = [{"month": k, "net": round(v, 2)} for k, v in sorted(by_month.items()) if k]
        out["by_side"] = {k: round(v, 2) for k, v in by_side.items()}

    if len(equity) >= 2:
        eq = np.array([e["equity"] for e in equity], float)
        peak = np.maximum.accumulate(eq)
        dd = eq / peak - 1.0
        out.update(
            start_equity=round(float(eq[0]), 2), equity=round(float(eq[-1]), 2),
            return_pct=round(float((eq[-1] / eq[0] - 1) * 100), 2),
            max_drawdown_pct=round(float(dd.min() * 100), 2),
            current_drawdown_pct=round(float(dd[-1] * 100), 2),
            drawdown_series=[round(float(x * 100), 3) for x in dd[-1000:]],
        )
    return out
