"""
Example short-term strategy: EMA(9)/EMA(21) trend crossover, filtered by RSI(14) to
avoid buying into overbought conditions, with ATR-based stop distance so the stop
adapts to current volatility instead of a fixed %.

This is a deliberately simple, well-understood strategy — not because it's
guaranteed to be profitable (no strategy is), but because a simple strategy you
fully understand and can reason about is worth more than a complex one you can't
debug when it starts losing. Treat this as a template to test, not a finished edge.

Signal contract expected by main.py / execution_manager:
    {"side": "buy"|"sell", "entry_price": float, "amount": float,
     "atr": float}  # atr included so stop distance can be volatility-aware
"""
import pandas as pd
import numpy as np


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = _ema(df["close"], 9)
    df["ema_slow"] = _ema(df["close"], 21)
    df["rsi"] = _rsi(df["close"], 14)
    df["atr"] = _atr(df, 14)
    df["volume_avg20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_avg20"]
    return df


def generate_signal(df: pd.DataFrame, risk_fraction_of_balance: float,
                     trading_balance: float, rsi_overbought=70, rsi_oversold=30,
                     volume_spike_threshold=1.5) -> dict | None:
    """
    df: OHLCV with at least 25 rows (needs warmup for EMA21/RSI14/ATR14/vol avg20).
    Returns a signal dict or None. Only ever proposes 'buy' entries here for
    simplicity — extend with short logic if your exchange/account supports it.

    Volume is used as a CONFIRMATION filter, not a standalone trigger: a volume
    spike with no trend signal is just noise (news, a large single order, etc).
    A crossover that also carries above-average volume is more likely to reflect
    real participation behind the move, not a thin, easily-reversed tick.
    """
    if len(df) < 25:
        return None

    df = compute_indicators(df)
    prev, last = df.iloc[-2], df.iloc[-1]

    crossed_up = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    not_overbought = last["rsi"] < rsi_overbought
    has_momentum = last["rsi"] > 45  # avoid buying a crossover with no real momentum behind it
    volume_confirmed = not pd.isna(last["volume_ratio"]) and last["volume_ratio"] >= volume_spike_threshold

    if crossed_up and not_overbought and has_momentum and volume_confirmed and not pd.isna(last["atr"]):
        proposed_amount = trading_balance * risk_fraction_of_balance
        return {
            "side": "buy",
            "entry_price": float(last["close"]),
            "amount": proposed_amount,
            "atr": float(last["atr"]),
            "volume_ratio": float(last["volume_ratio"]),
        }
    return None


def generate_signal_with_ml(df: pd.DataFrame, risk_fraction_of_balance: float,
                             trading_balance: float, lstm_predictor=None,
                             ml_min_confidence: float = 0.6, **kwargs) -> dict | None:
    """
    Optional variant: only fires a signal if BOTH the existing rule-based logic
    (generate_signal, unchanged above) agrees AND the LSTM predictor's probability
    of an upward move clears `ml_min_confidence`. The LSTM is used purely as an
    additional filter that can only make the system more conservative (fewer
    trades) — it can never trigger a trade the rule-based logic wouldn't have
    already flagged. This keeps the well-understood, debuggable rule-based logic
    as the primary gate, with the LSTM as a second opinion rather than a
    replacement decision-maker.

    Pass lstm_predictor=None to skip the ML filter entirely and behave exactly
    like generate_signal().
    """
    base_signal = generate_signal(df, risk_fraction_of_balance, trading_balance, **kwargs)
    if base_signal is None or lstm_predictor is None:
        return base_signal

    prob_up = lstm_predictor.predict_proba(df)
    if prob_up is None:
        # model not trained/loaded yet — fail safe by not adding an unverified filter,
        # same as passing lstm_predictor=None
        return base_signal

    if prob_up < ml_min_confidence:
        return None  # rule-based logic said buy, but the model disagrees enough to skip

    base_signal["ml_confidence"] = round(prob_up, 3)
    return base_signal
