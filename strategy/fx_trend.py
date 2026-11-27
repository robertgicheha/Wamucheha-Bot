"""
Daily time-series-momentum (trend-following) strategy for FX majors/crosses.

PURE FUNCTIONS ONLY: no broker calls, no I/O, no clock. The same functions feed
strategy/fx_trend_backtest.py and (later) a live runner, so a backtest describes
what live trading would do.

Rules (all evaluated on a CLOSED daily bar; trades are filled at the NEXT open).
Two entry/exit rule sets, picked by TrendParams.rules:

"breakout" (long/flat, same rules as strategy/crypto_trend.py):
  long  after a close above the prior breakout_len-bar high AND above SMA(sma_len)
  flat  after a close below the prior exit_len-bar low OR below SMA(sma_len)

"momentum" (default):
  long  when close > SMA(sma_len)  AND  close / close[mom_len] - 1 > 0
  short when close < SMA(sma_len)  AND  close / close[mom_len] - 1 < 0
  flat  otherwise (the two filters disagree)
Risk: initial stop = atr_stop_mult * ATR(atr_len) from entry, ratcheting
(chandelier) in the trade's favour; size so that a stop-out loses risk_pct of
equity.

Nothing here is a promise of profit. Trend following loses in long chop and
earns its money in a few large trends.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategy.crypto_trend import TrendConfig, compute_target_exposure


@dataclass(frozen=True)
class TrendParams:
    sma_len: int = 100
    mom_len: int = 90
    atr_len: int = 14
    atr_stop_mult: float = 3.0
    risk_pct: float = 0.5          # % of equity lost if the initial stop is hit
    allow_short: bool = True
    max_leverage_per_pos: float = 5.0   # cap on notional / equity for one position
    rules: str = "momentum"        # "momentum" | "breakout"
    breakout_len: int = 55         # breakout rules only
    exit_len: int = 20             # breakout rules only

    @property
    def warmup(self) -> int:
        extra = (self.breakout_len, self.exit_len) if self.rules == "breakout" else ()
        return max(self.sma_len, self.mom_len, self.atr_len, *extra) + 1


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR. Row t uses data up to and including bar t only."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def compute_indicators(df: pd.DataFrame, p: TrendParams = TrendParams()) -> pd.DataFrame:
    """Return a copy with sma, mom, atr and signal (+1/-1/0) columns.

    signal[t] depends only on bars <= t (no look-ahead). It is NaN-safe: during
    warm-up the signal is 0.
    """
    out = df.copy()
    out["sma"] = out["close"].rolling(p.sma_len, min_periods=p.sma_len).mean()
    out["mom"] = out["close"] / out["close"].shift(p.mom_len) - 1.0
    out["atr"] = atr(out, p.atr_len)

    if p.rules == "breakout":
        exposure = compute_target_exposure(out, TrendConfig(
            breakout_days=p.breakout_len, exit_days=p.exit_len,
            trend_ma_days=p.sma_len, atr_days=p.atr_len))
        ready = out[["sma", "mom", "atr"]].notna().all(axis=1)
        out["signal"] = (exposure > 0).astype(int).where(ready, 0).astype(int)
        return out
    if p.rules != "momentum":
        raise ValueError(f"unknown strategy rules: {p.rules!r}")

    long_ = (out["close"] > out["sma"]) & (out["mom"] > 0)
    short_ = (out["close"] < out["sma"]) & (out["mom"] < 0) if p.allow_short else False
    sig = np.where(long_, 1, np.where(short_, -1, 0))
    out["signal"] = pd.Series(sig, index=out.index).where(out[["sma", "mom", "atr"]].notna().all(axis=1), 0).astype(int)
    return out


def initial_stop(side: int, entry: float, atr_value: float, mult: float) -> float:
    return entry - side * mult * atr_value


def trail_stop(side: int, current_stop: float, bar_high: float, bar_low: float,
               atr_value: float, mult: float) -> float:
    """Chandelier trail: only ever tightens, never loosens."""
    if side > 0:
        return max(current_stop, bar_high - mult * atr_value)
    return min(current_stop, bar_low + mult * atr_value)


def position_notional(equity: float, risk_pct: float, entry: float, stop: float,
                      max_leverage: float) -> float:
    """Notional (account currency) such that hitting `stop` loses risk_pct% of equity."""
    stop_frac = abs(entry - stop) / entry if entry > 0 else 0.0
    if equity <= 0 or stop_frac <= 0:
        return 0.0
    notional = equity * (risk_pct / 100.0) / stop_frac
    return min(notional, equity * max_leverage)


def currency_legs(pair: str, side: int) -> dict[str, int]:
    """'EURUSD' long -> {'EUR': +1, 'USD': -1}. Used to cap one-sided currency bets."""
    pair = pair.upper().replace("/", "").replace("_", "")[:6]
    base, quote = pair[:3], pair[3:6]
    return {base: side, quote: -side}


def latest_signals(frames: dict[str, pd.DataFrame], p: TrendParams = TrendParams()) -> dict[str, dict]:
    """Live helper: call with CLOSED daily bars only (drop the forming bar).

    Returns {pair: {"side": +1/-1/0, "atr": float, "close": float, "stop_dist": float}}.
    """
    out = {}
    for pair, df in frames.items():
        if len(df) < p.warmup:
            continue
        ind = compute_indicators(df, p)
        last = ind.iloc[-1]
        out[pair] = {
            "side": int(last["signal"]),
            "atr": float(last["atr"]),
            "close": float(last["close"]),
            "stop_dist": float(p.atr_stop_mult * last["atr"]),
        }
    return out
