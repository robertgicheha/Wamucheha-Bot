"""
Multi-strategy ensemble with EMA, MACD, Bollinger Bands, RSI, and Momentum.

The ensemble votes: each sub-strategy produces a buy/sell/neutral signal with
a confidence score. Signals are weighted and summed — if the total score exceeds
a threshold, a trade signal is produced. This is far more robust than relying
on a single indicator.

Also supports short/sell entries for exchanges that support them (OANDA, MT5, etc).

Signal contract expected by main.py / execution_manager:
    {"side": "buy"|"sell", "entry_price": float, "amount": float,
     "atr": float, "score": float, "strategies": list[str]}
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


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (0-100)."""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.rolling(period).mean()


def _stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    """Stochastic Oscillator — %K and %D."""
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def compute_indicators(df: pd.DataFrame, cfg: dict = None) -> pd.DataFrame:
    """Compute all technical indicators used by the strategy ensemble."""
    cfg = cfg or {}
    ema_fast_period = cfg.get("ema_fast", 9)
    ema_slow_period = cfg.get("ema_slow", 21)
    rsi_period = cfg.get("rsi_period", 14)

    df = df.copy()
    # Trend
    df["ema_fast"] = _ema(df["close"], ema_fast_period)
    df["ema_slow"] = _ema(df["close"], ema_slow_period)
    df["ema_50"] = _ema(df["close"], 50)
    df["ema_200"] = _ema(df["close"], 200)

    # Momentum
    df["rsi"] = _rsi(df["close"], rsi_period)
    df["macd"], df["macd_signal"], df["macd_hist"] = _macd(df["close"])
    df["adx"] = _adx(df)
    df["stoch_k"], df["stoch_d"] = _stochastic(df)

    # Volatility
    df["atr"] = _atr(df, 14)
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = _bollinger_bands(df["close"])

    # Volume
    df["volume_avg20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_avg20"]

    # OBV (On-Balance Volume) for volume confirmation
    df["obv"] = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    df["obv_ema"] = _ema(df["obv"], 20)

    return df


# --------------- Individual strategy scorers ---------------
# Each returns (buy_score, sell_score) in range [-1, 1].
# Positive buy_score = bullish. Positive sell_score = bearish.

def _score_ema_crossover(df: pd.DataFrame) -> tuple[float, float]:
    """EMA(9)/EMA(21) crossover with EMA(50) trend filter."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    # Bullish crossover
    if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
        buy = 0.8
        if last["close"] > last["ema_50"]:
            buy = 1.0  # strong: crossover + above trend

    # Bearish crossover
    if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
        sell = 0.8
        if last["close"] < last["ema_50"]:
            sell = 1.0

    # Continuation: already in trend
    if buy == 0 and sell == 0:
        if last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_50"]:
            buy = 0.3  # mild bullish continuation
        elif last["ema_fast"] < last["ema_slow"] and last["close"] < last["ema_50"]:
            sell = 0.3

    return buy, sell


def _score_macd(df: pd.DataFrame) -> tuple[float, float]:
    """MACD histogram crossover + divergence detection."""
    prev, last = df.iloc[-2], df.iloc[-1]
    prev2 = df.iloc[-3] if len(df) > 2 else prev
    buy, sell = 0.0, 0.0

    # MACD histogram crosses zero from below (bullish)
    if prev["macd_hist"] <= 0 and last["macd_hist"] > 0:
        buy = 0.9
    elif prev["macd_hist"] >= 0 and last["macd_hist"] < 0:
        sell = 0.9

    # MACD line crosses signal line
    if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        buy = max(buy, 0.7)
    if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        sell = max(sell, 0.7)

    # Bullish divergence: price makes lower low but MACD makes higher low
    if last["close"] < prev["close"] and last["macd_hist"] > prev["macd_hist"] and last["macd_hist"] > prev2["macd_hist"]:
        buy = max(buy, 0.6)

    return buy, sell


def _score_bollinger(df: pd.DataFrame) -> tuple[float, float]:
    """Bollinger Band mean-reversion signals."""
    last = df.iloc[-1]
    buy, sell = 0.0, 0.0

    bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
    price_position = (last["close"] - last["bb_lower"]) / (last["bb_upper"] - last["bb_lower"]) \
        if (last["bb_upper"] - last["bb_lower"]) > 0 else 0.5

    # Price touches/breaks lower band = potential buy (mean reversion)
    if price_position <= 0.05 and last["rsi"] < 40:
        buy = 0.8
    elif price_position <= 0.15 and last["rsi"] < 45:
        buy = 0.5

    # Price touches/breaks upper band = potential sell
    if price_position >= 0.95 and last["rsi"] > 60:
        sell = 0.8
    elif price_position >= 0.85 and last["rsi"] > 55:
        sell = 0.5

    # Squeeze: very narrow bands = breakout imminent, wait for direction
    if bb_width < 0.02:
        buy *= 0.5
        sell *= 0.5

    return buy, sell


def _score_rsi(df: pd.DataFrame, overbought: int = 70, oversold: int = 30) -> tuple[float, float]:
    """RSI extremes + divergence."""
    prev, last = df.iloc[-2], df.iloc[-1]
    prev3 = df.iloc[-4] if len(df) > 3 else prev
    buy, sell = 0.0, 0.0

    # RSI oversold bounce
    if last["rsi"] < oversold:
        buy = 0.7
    elif last["rsi"] < 40:
        buy = 0.3

    # RSI overbought
    if last["rsi"] > overbought:
        sell = 0.7
    elif last["rsi"] > 60:
        sell = 0.3

    # Bullish divergence: price lower low, RSI higher low
    if last["close"] < prev3["close"] and last["rsi"] > prev3["rsi"] and last["rsi"] < 45:
        buy = max(buy, 0.6)

    # Bearish divergence: price higher high, RSI lower high
    if last["close"] > prev3["close"] and last["rsi"] < prev3["rsi"] and last["rsi"] > 55:
        sell = max(sell, 0.6)

    return buy, sell


def _score_momentum(df: pd.DataFrame) -> tuple[float, float]:
    """Multi-factor momentum: ADX trend strength + Stochastic + Volume + OBV."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    # ADX: strong trend (>25) confirms directional trades
    adx_strong = not pd.isna(last["adx"]) and last["adx"] > 25

    # Stochastic crossover in oversold/overbought zones
    if prev["stoch_k"] <= prev["stoch_d"] and last["stoch_k"] > last["stoch_d"] and last["stoch_k"] < 30:
        buy = 0.7
    if prev["stoch_k"] >= prev["stoch_d"] and last["stoch_k"] < last["stoch_d"] and last["stoch_k"] > 70:
        sell = 0.7

    # Volume confirmation
    vol_confirmed = not pd.isna(last["volume_ratio"]) and last["volume_ratio"] > 1.2

    # OBV trend
    obv_bullish = not pd.isna(last["obv_ema"]) and last["obv"] > last["obv_ema"]
    obv_bearish = not pd.isna(last["obv_ema"]) and last["obv"] < last["obv_ema"]

    if adx_strong and vol_confirmed:
        if buy > 0 and obv_bullish:
            buy = min(buy * 1.3, 1.0)
        if sell > 0 and obv_bearish:
            sell = min(sell * 1.3, 1.0)

    return buy, sell


# --------------- Main ensemble ---------------

def generate_signal(df: pd.DataFrame, risk_fraction_of_balance: float,
                     trading_balance: float, cfg: dict = None) -> dict | None:
    """
    Multi-strategy ensemble signal generator.

    Runs 5 sub-strategies (EMA crossover, MACD, Bollinger, RSI, Momentum),
    weights their scores, and produces a buy or sell signal if the weighted
    score exceeds the threshold.

    cfg: strategy config dict from config.yaml (strategy section).
    """
    if len(df) < 50:
        return None

    cfg = cfg or {}
    weights = cfg.get("weights", {
        "ema_crossover": 0.25, "macd": 0.25, "bollinger": 0.20,
        "rsi_divergence": 0.15, "momentum": 0.15,
    })
    min_score = cfg.get("min_signal_score", 3) / 5.0  # normalize to 0-1
    enable_shorts = cfg.get("enable_shorts", True)

    df = compute_indicators(df, cfg)

    if pd.isna(df.iloc[-1]["atr"]):
        return None

    # Collect scores from each strategy
    buy_scores = []
    sell_scores = []

    s = _score_ema_crossover(df)
    buy_scores.append(s[0] * weights.get("ema_crossover", 0.25))
    sell_scores.append(s[1] * weights.get("ema_crossover", 0.25))

    s = _score_macd(df)
    buy_scores.append(s[0] * weights.get("macd", 0.25))
    sell_scores.append(s[1] * weights.get("macd", 0.25))

    s = _score_bollinger(df)
    buy_scores.append(s[0] * weights.get("bollinger", 0.20))
    sell_scores.append(s[1] * weights.get("bollinger", 0.20))

    s = _score_rsi(df, cfg.get("rsi_overbought", 70), cfg.get("rsi_oversold", 30))
    buy_scores.append(s[0] * weights.get("rsi_divergence", 0.15))
    sell_scores.append(s[1] * weights.get("rsi_divergence", 0.15))

    s = _score_momentum(df)
    buy_scores.append(s[0] * weights.get("momentum", 0.15))
    sell_scores.append(s[1] * weights.get("momentum", 0.15))

    total_buy = sum(buy_scores)
    total_sell = sum(sell_scores)
    last = df.iloc[-1]

    # Determine which strategies contributed to the signal
    strategy_names = ["ema_crossover", "macd", "bollinger", "rsi_divergence", "momentum"]
    raw_scores = [_score_ema_crossover(df), _score_macd(df), _score_bollinger(df),
                  _score_rsi(df), _score_momentum(df)]

    proposed_amount = trading_balance * risk_fraction_of_balance

    # Buy signal
    if total_buy >= min_score and total_buy > total_sell:
        active = [name for name, (b, _) in zip(strategy_names, raw_scores) if b > 0.3]
        return {
            "side": "buy",
            "entry_price": float(last["close"]),
            "amount": proposed_amount,
            "atr": float(last["atr"]),
            "score": round(total_buy, 3),
            "strategies": active,
        }

    # Sell/short signal
    if enable_shorts and total_sell >= min_score and total_sell > total_buy:
        active = [name for name, (_, s) in zip(strategy_names, raw_scores) if s > 0.3]
        return {
            "side": "sell",
            "entry_price": float(last["close"]),
            "amount": proposed_amount,
            "atr": float(last["atr"]),
            "score": round(total_sell, 3),
            "strategies": active,
        }

    return None


def generate_exit_signal(df: pd.DataFrame, position: dict,
                          trailing_activate_pct: float = 1.5,
                          trailing_distance_pct: float = 1.0,
                          cfg: dict = None) -> dict | None:
    """
    Proactive exit signal — fires BEFORE the fixed TP/SL levels if the
    technical picture deteriorates. Uses the full indicator set for robust exits.

    Exit conditions:
    1. Trend reversal: EMA crossover against position direction.
    2. RSI exhaustion: RSI > 80 (longs) or < 20 (shorts).
    3. Trailing stop: price rises then drops too much from peak.
    4. MACD death cross / golden cross against position.
    5. Bollinger band mean reversion.

    Returns {"exit_price": float, "reason": str} or None.
    """
    if len(df) < 50:
        return None

    cfg = cfg or {}
    df = compute_indicators(df, cfg)
    prev, last = df.iloc[-2], df.iloc[-1]
    entry_price = position["entry_price"]
    side = position["side"]
    peak_price = position.get("peak_price", entry_price)
    last_price = float(last["close"])

    if side == "buy":
        # 1. EMA trend reversal
        if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
            return {"exit_price": last_price, "reason": "trend_reversal_ema_cross"}

        # 2. RSI exhaustion
        if not pd.isna(last["rsi"]) and last["rsi"] > 80:
            return {"exit_price": last_price, "reason": "rsi_overbought_exhaustion"}

        # 3. MACD death cross
        if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
            if not pd.isna(last["adx"]) and last["adx"] > 20:
                return {"exit_price": last_price, "reason": "macd_death_cross"}

        # 4. Trailing stop
        profit_pct = (peak_price - entry_price) / entry_price * 100
        if profit_pct >= trailing_activate_pct:
            drawdown_from_peak = (peak_price - last_price) / peak_price * 100
            if drawdown_from_peak >= trailing_distance_pct:
                return {"exit_price": last_price, "reason": "trailing_stop_hit"}

        # 5. Price breaks below lower Bollinger after touching upper (mean reversion)
        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width > 0.03 and last_price < last["bb_lower"]:
            return {"exit_price": last_price, "reason": "bollinger_lower_break"}

    else:  # short/sell
        # 1. EMA trend reversal
        if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
            return {"exit_price": last_price, "reason": "trend_reversal_ema_cross"}

        # 2. RSI exhaustion
        if not pd.isna(last["rsi"]) and last["rsi"] < 20:
            return {"exit_price": last_price, "reason": "rsi_oversold_exhaustion"}

        # 3. MACD golden cross
        if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
            if not pd.isna(last["adx"]) and last["adx"] > 20:
                return {"exit_price": last_price, "reason": "macd_golden_cross"}

        # 4. Trailing stop for shorts
        profit_pct = (entry_price - peak_price) / entry_price * 100
        if profit_pct >= trailing_activate_pct:
            drawdown_from_peak = (last_price - peak_price) / peak_price * 100
            if drawdown_from_peak >= trailing_distance_pct:
                return {"exit_price": last_price, "reason": "trailing_stop_hit"}

        # 5. Price breaks above upper Bollinger (mean reversion)
        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width > 0.03 and last_price > last["bb_upper"]:
            return {"exit_price": last_price, "reason": "bollinger_upper_break"}

    return None


def generate_signal_with_ml(df: pd.DataFrame, risk_fraction_of_balance: float,
                             trading_balance: float, lstm_predictor=None,
                             ml_min_confidence: float = 0.6,
                             cfg: dict = None, **kwargs) -> dict | None:
    """
    Multi-strategy ensemble + optional ML filter.

    The ensemble produces a signal, then the LSTM can veto it if confidence is
    too low. The LSTM can only reduce trades, never add them.
    """
    base_signal = generate_signal(df, risk_fraction_of_balance, trading_balance, cfg=cfg)
    if base_signal is None or lstm_predictor is None:
        return base_signal

    prob_up = lstm_predictor.predict_proba(df)
    if prob_up is None:
        return base_signal

    # For buy signals, check prob_up; for sell signals, check prob_down
    if base_signal["side"] == "buy":
        if prob_up < ml_min_confidence:
            return None
        base_signal["ml_confidence"] = round(prob_up, 3)
    else:
        if (1 - prob_up) < ml_min_confidence:
            return None
        base_signal["ml_confidence"] = round(1 - prob_up, 3)

    return base_signal
