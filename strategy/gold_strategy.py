"""Gold-specific trading strategy enhancements for XAUUSD (MT5)."""

from datetime import datetime, time
from typing import Optional, Tuple

import pandas as pd


def get_session(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = datetime.utcnow()
    t = dt.time()
    if time(13, 0) <= t < time(17, 0):
        return "london_ny_overlap"
    if time(8, 0) <= t < time(13, 0):
        return "london"
    if time(13, 0) <= t < time(22, 0):
        return "ny"
    if time(0, 0) <= t < time(9, 0):
        return "asian"
    return "other"


def is_liquid_session(dt: Optional[datetime] = None) -> bool:
    if dt is None:
        dt = datetime.utcnow()
    t = dt.time()
    if time(13, 0) <= t <= time(17, 0):
        return True
    if time(8, 0) <= t <= time(11, 0):
        return True
    return False


def calculate_atr_levels(atr: float, entry_price: float, sl_mult: float = 2.0, tp_mult: float = 2.5, risk_reward_min: float = 1.5) -> Tuple[float, float]:
    sl = atr * sl_mult
    tp = atr * tp_mult
    if sl <= 0:
        return entry_price * 0.98, entry_price * 1.025
    if tp / sl < risk_reward_min:
        tp = sl * risk_reward_min
    return entry_price - sl, entry_price + tp

def ema_trend_signal(df, fast=9, slow=21):
    if len(df) < slow + 2:
        return (0.0, 0.0)
    ema_f = df["close"].ewm(span=fast, adjust=False).mean()
    ema_s = df["close"].ewm(span=slow, adjust=False).mean()
    if pd.isna(ema_f.iloc[-1]) or pd.isna(ema_s.iloc[-1]):
        return (0.0, 0.0)
    if ema_f.iloc[-1] > ema_s.iloc[-1] and ema_f.iloc[-2] <= ema_s.iloc[-2]:
        return (0.4, 0.0)
    if ema_f.iloc[-1] < ema_s.iloc[-1] and ema_f.iloc[-2] >= ema_s.iloc[-2]:
        return (0.0, 0.4)
    if ema_f.iloc[-1] > ema_s.iloc[-1]:
        return (0.25, 0.0)
    if ema_f.iloc[-1] < ema_s.iloc[-1]:
        return (0.0, 0.25)
    return (0.0, 0.0)

def breakout_confirmation(df, lookback=20, atr_mult=1.0):
    if len(df) < lookback + 5 or "atr" not in df.columns:
        return (0.0, 0.0)
    high = df["high"].rolling(lookback).max()
    low = df["low"].rolling(lookback).min()
    atr = df["atr"].iloc[-1]
    last_close = df["close"].iloc[-1]
    if pd.isna(atr) or pd.isna(high.iloc[-2]) or pd.isna(low.iloc[-2]):
        return (0.0, 0.0)
    if last_close > high.iloc[-2] + atr * atr_mult:
        return (0.3, 0.0)
    if last_close < low.iloc[-2] - atr * atr_mult:
        return (0.0, 0.3)
    return (0.0, 0.0)


def atr_sl_tp(df, side="buy", sl_mult=2.0, tp_mult=2.5, rr_min=1.5):
    if "atr" not in df.columns or pd.isna(df["atr"].iloc[-1]):
        atr = (df["high"].iloc[-1] - df["low"].iloc[-1]) * 0.6
    else:
        atr = df["atr"].iloc[-1]
    entry = df["close"].iloc[-1]
    if side == "buy":
        sl = entry - atr * sl_mult
        tp = entry + atr * tp_mult
        if sl <= 0 or (tp - entry) / (entry - sl) < rr_min:
            tp = entry + (entry - sl) * rr_min
        return entry, sl, tp
    sl = entry + atr * sl_mult
    tp = entry - atr * tp_mult
    if tp >= entry:
        tp = entry - atr * tp_mult
    return entry, sl, tp
