"""
Daily trend-following (time-series momentum) for liquid crypto majors.

Rules (long/flat by default; shorts optional because spot-only accounts can't):
  * ENTER long at the next open after a close above the `breakout_days`
    Donchian high, provided close is also above the `trend_ma_days` SMA
  * EXIT at the next open after a close below the `exit_days` Donchian low
    or below the SMA
  * SIZE by volatility: risk `risk_per_trade_pct` of equity against a stop
    `atr_stop_mult` x ATR away, capped at `max_leverage` x equity

Signals are computed on the close of bar t and executed at the open of bar
t+1, never on the same bar, so the backtest cannot trade on information it
would not have had. Costs (fee + slippage) are charged on every change in
notional. Expect long flat stretches and many small losses in chop; the payoff
comes from a few large moves.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class TrendConfig:
    breakout_days: int = 55
    exit_days: int = 20
    trend_ma_days: int = 100
    atr_days: int = 20
    atr_stop_mult: float = 3.0
    risk_per_trade_pct: float = 0.75   # of equity, within the 0.5-1% guidance
    max_leverage: float = 1.0          # 1x = spot-equivalent; keep <= 3
    allow_short: bool = False
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    funding_bps_per_day: float = 0.0   # carry cost on the leveraged part (>1x) of perp longs


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def compute_target_exposure(df: pd.DataFrame, cfg: TrendConfig = None) -> pd.Series:
    """Signed target exposure (multiple of equity), decided at each bar's close.

    The caller must apply it from the NEXT bar. 0 while indicators warm up.
    """
    cfg = cfg or TrendConfig()
    close = df["close"]
    don_hi = df["high"].rolling(cfg.breakout_days).max().shift(1)   # excludes today
    don_lo_entry = df["low"].rolling(cfg.breakout_days).min().shift(1)
    don_lo_exit = df["low"].rolling(cfg.exit_days).min().shift(1)
    don_hi_exit = df["high"].rolling(cfg.exit_days).max().shift(1)
    sma = close.rolling(cfg.trend_ma_days).mean()
    atr = _atr(df, cfg.atr_days)

    stop_dist = (cfg.atr_stop_mult * atr / close).replace(0, np.nan)
    size = (cfg.risk_per_trade_pct / 100.0 / stop_dist).clip(upper=cfg.max_leverage).fillna(0.0)

    state = 0
    out = np.zeros(len(df))
    for i in range(len(df)):
        if np.isnan(sma.iloc[i]) or np.isnan(don_hi.iloc[i]) or np.isnan(atr.iloc[i]):
            continue
        c = close.iloc[i]
        if state <= 0 and c > don_hi.iloc[i] and c > sma.iloc[i]:
            state = 1
        elif state == 1 and (c < don_lo_exit.iloc[i] or c < sma.iloc[i]):
            state = 0
        if cfg.allow_short:
            if state >= 0 and c < don_lo_entry.iloc[i] and c < sma.iloc[i]:
                state = -1
            elif state == -1 and (c > don_hi_exit.iloc[i] or c > sma.iloc[i]):
                state = 0
        out[i] = state * size.iloc[i]
    return pd.Series(out, index=df.index)


def backtest_trend(df: pd.DataFrame, cfg: TrendConfig = None,
                   initial_balance: float = 1000.0) -> dict:
    """df: daily OHLC indexed by time, columns open/high/low/close."""
    cfg = cfg or TrendConfig()
    target = compute_target_exposure(df, cfg)
    exposure = target.shift(1).fillna(0.0)      # decided at t-1 close, applied at bar t open
    cost_rate = (cfg.fee_bps + cfg.slippage_bps) / 10_000.0

    equity = initial_balance
    curve = np.full(len(df), initial_balance, dtype=float)
    held = 0.0
    trades, entry_eq, entered = [], 0.0, None
    costs_paid = 0.0
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()

    for i in range(1, len(df)):
        want = exposure.iloc[i]
        # Rebalance at the open. Only trade when direction flips or size moves >25%,
        # so small ATR drift doesn't bleed fees every day.
        if np.sign(want) != np.sign(held) or abs(want - held) > 0.25 * max(abs(held), 1e-9):
            cost = abs(want - held) * equity * cost_rate
            equity -= cost
            costs_paid += cost
            if held == 0 and want != 0:
                entered, entry_eq = df.index[i], equity
            elif want == 0 and held != 0:
                trades.append({"entry": entered, "exit": df.index[i],
                               "net_pnl": equity - entry_eq})
            held = want
        equity *= 1 + held * (closes[i] / opens[i] - 1)             # intraday leg
        if abs(held) > 1:                                           # carry on leveraged part
            equity -= (abs(held) - 1) * equity * cfg.funding_bps_per_day / 10_000.0
        if i + 1 < len(df):
            equity *= 1 + held * (opens[i + 1] / closes[i] - 1)     # overnight gap leg
        curve[i] = equity
    if held != 0:
        trades.append({"entry": entered, "exit": df.index[-1],
                       "net_pnl": equity - entry_eq, "open_at_end": True})

    eq = pd.Series(curve, index=df.index)
    rets = eq.pct_change().dropna()
    years = max((df.index[-1] - df.index[0]).days / 365.25, 1e-9)
    max_dd = float(((eq - eq.cummax()) / eq.cummax()).min() * 100)
    wins = [t for t in trades if t["net_pnl"] > 0]
    return {
        "final_balance": round(float(eq.iloc[-1]), 2),
        "total_return_pct": round((eq.iloc[-1] / initial_balance - 1) * 100, 2),
        "annualized_return_pct": round(((eq.iloc[-1] / initial_balance) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "costs_paid": round(costs_paid, 2),
        "buy_hold_return_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
        "equity": eq,
        "trade_log": trades,
    }
