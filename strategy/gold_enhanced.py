"""Advanced Gold Trading Strategies Module

Implements session-based filtering, ATR-based risk management,
breakout confirmation, and news-aware controls.
"""
from datetime import datetime, time

def is_in_liquid_session(dt):
    """Check if in high-liquidity session for XAUUSD."""
    t = dt.time()
    if time(13, 0) <= t <= time(17, 0) or time(8, 0) <= t <= time(11, 0):
        return True
    return False

def atr_multiplier_sl_tp(atr, mult_sl=2.0, mult_tp=3.0, min_risk_rew=1.5):
    """Calculate SL/TP based on ATR with risk/reward check."""
    sl = atr * mult_sl
    tp = atr * mult_tp
    if tp / sl < min_risk_rew:
        tp = sl * min_risk_rew
    return sl, tp

