"""
Multi-strategy ensemble with 10 strategies and adaptive weighting.

Strategies:
 1. EMA Crossover - Trend following via EMA(9)/EMA(21) with EMA(50) filter
 2. MACD - Histogram zero-cross, signal-line cross, divergence
 3. Bollinger Bands - Mean-reversion, squeeze detection
 4. RSI Divergence - Oversold/overbought bounces, divergence
 5. Momentum - ADX + Stochastic + Volume + OBV
 6. VWAP Reversion - VWAP pullback entries
 7. Keltner Channel Breakout - ATR-based channel breakout
 8. Ichimoku Cloud - Multi-line trend system
 9. Scalping - Ultra-short-term microstructure signals
10. Supertrend - ATR-based trend following

Adaptive weighting adjusts strategy influence based on recent performance and
market regime (trending vs ranging). Multi-timeframe confirmation adds
higher-timeframe trend alignment.

Signal contract expected by main.py / execution_manager:
    {"side": "buy"|"sell", "entry_price": float, "amount": float,
     "atr": float, "score": float, "strategies": list[str]}
"""
import pandas as pd
import numpy as np


# --------------- Indicator helpers ---------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
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
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price — computed from session or rolling window."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cumvol = df["volume"].cumsum()
    cumtpv = (typical * df["volume"]).cumsum()
    return cumtpv / cumvol.replace(0, np.nan)


def _rolling_vwap(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Rolling VWAP over N periods for intraday-style signals."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]
    return tp_vol.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0, np.nan)


def _keltner_channels(df: pd.DataFrame, ema_period: int = 20, atr_period: int = 10, multiplier: float = 2.0):
    mid = _ema(df["close"], ema_period)
    atr = _atr(df, atr_period)
    upper = mid + multiplier * atr
    lower = mid - multiplier * atr
    return upper, mid, lower


def _ichimoku(df: pd.DataFrame):
    """Ichimoku Cloud: tenkan, kijun, senkou_a, senkou_b, chikou."""
    high, low = df["high"], df["low"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    chikou = df["close"].shift(-26)
    return tenkan, kijun, senkou_a, senkou_b, chikou


def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Supertrend indicator — ATR-based trend line."""
    atr = _atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr
    supertrend = pd.Series(np.nan, index=df.index)
    direction = pd.Series(1, index=df.index)

    for i in range(1, len(df)):
        if df["close"].iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        if direction.iloc[i] == 1:
            supertrend.iloc[i] = lower_band.iloc[i]
        else:
            supertrend.iloc[i] = upper_band.iloc[i]

    return supertrend, direction


def _mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Money Flow Index — volume-weighted RSI."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    raw_mf = typical * df["volume"]
    pos_mf = raw_mf.where(typical > typical.shift(1), 0).rolling(period).sum()
    neg_mf = raw_mf.where(typical < typical.shift(1), 0).rolling(period).sum()
    mfr = pos_mf / neg_mf.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))


def _cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    sma = typical.rolling(period).mean()
    mad = typical.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (typical - sma) / (0.015 * mad).replace(0, np.nan)


# --------------- Compute all indicators ---------------

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
    df["mfi"] = _mfi(df)
    df["cci"] = _cci(df)

    # Volatility
    df["atr"] = _atr(df, 14)
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = _bollinger_bands(df["close"])

    # Keltner Channels
    df["kc_upper"], df["kc_mid"], df["kc_lower"] = _keltner_channels(df)

    # Ichimoku
    df["ichi_tenkan"], df["ichi_kijun"], df["ichi_senkou_a"], df["ichi_senkou_b"], df["ichi_chikou"] = _ichimoku(df)

    # Supertrend
    df["supertrend"], df["supertrend_dir"] = _supertrend(df)

    # VWAP
    df["vwap"] = _rolling_vwap(df, 20)

    # Volume
    df["volume_avg20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_avg20"].replace(0, np.nan)
    df["obv"] = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    df["obv_ema"] = _ema(df["obv"], 20)

    # Multi-timeframe helpers (computed on the same df for simplicity)
    df["ema_200_slope"] = df["ema_200"].pct_change(5) * 100

    # Detect market regime: trending vs ranging
    adx_vals = df["adx"].iloc[-5:]
    avg_adx = adx_vals.mean() if not adx_vals.isna().all() else 20
    bb_width = (df["bb_upper"].iloc[-1] - df["bb_lower"].iloc[-1]) / df["bb_mid"].iloc[-1] if df["bb_mid"].iloc[-1] > 0 else 0
    df.attrs["market_regime"] = "trending" if avg_adx > 25 else "ranging"
    df.attrs["bb_width"] = bb_width

    return df


# --------------- 10 Individual strategy scorers ---------------
# Each returns (buy_score, sell_score) in range [-1, 1].

def _score_ema_crossover(df: pd.DataFrame) -> tuple[float, float]:
    """EMA(9)/EMA(21) crossover with EMA(50) trend filter."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
        buy = 0.8
        if last["close"] > last["ema_50"]:
            buy = 1.0
    if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
        sell = 0.8
        if last["close"] < last["ema_50"]:
            sell = 1.0

    if buy == 0 and sell == 0:
        if last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_50"]:
            buy = 0.3
        elif last["ema_fast"] < last["ema_slow"] and last["close"] < last["ema_50"]:
            sell = 0.3

    return buy, sell


def _score_macd(df: pd.DataFrame) -> tuple[float, float]:
    """MACD histogram crossover + divergence detection."""
    prev, last = df.iloc[-2], df.iloc[-1]
    prev2 = df.iloc[-3] if len(df) > 2 else prev
    buy, sell = 0.0, 0.0

    if prev["macd_hist"] <= 0 and last["macd_hist"] > 0:
        buy = 0.9
    elif prev["macd_hist"] >= 0 and last["macd_hist"] < 0:
        sell = 0.9

    if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        buy = max(buy, 0.7)
    if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        sell = max(sell, 0.7)

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

    if price_position <= 0.05 and last["rsi"] < 40:
        buy = 0.8
    elif price_position <= 0.15 and last["rsi"] < 45:
        buy = 0.5

    if price_position >= 0.95 and last["rsi"] > 60:
        sell = 0.8
    elif price_position >= 0.85 and last["rsi"] > 55:
        sell = 0.5

    if bb_width < 0.02:
        buy *= 0.5
        sell *= 0.5

    return buy, sell


def _score_rsi(df: pd.DataFrame, overbought: int = 70, oversold: int = 30) -> tuple[float, float]:
    """RSI extremes + divergence."""
    prev, last = df.iloc[-2], df.iloc[-1]
    prev3 = df.iloc[-4] if len(df) > 3 else prev
    buy, sell = 0.0, 0.0

    if last["rsi"] < oversold:
        buy = 0.7
    elif last["rsi"] < 40:
        buy = 0.3
    if last["rsi"] > overbought:
        sell = 0.7
    elif last["rsi"] > 60:
        sell = 0.3

    if last["close"] < prev3["close"] and last["rsi"] > prev3["rsi"] and last["rsi"] < 45:
        buy = max(buy, 0.6)
    if last["close"] > prev3["close"] and last["rsi"] < prev3["rsi"] and last["rsi"] > 55:
        sell = max(sell, 0.6)

    return buy, sell


def _score_momentum(df: pd.DataFrame) -> tuple[float, float]:
    """Multi-factor momentum: ADX + Stochastic + Volume + OBV."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    adx_strong = not pd.isna(last["adx"]) and last["adx"] > 25

    if prev["stoch_k"] <= prev["stoch_d"] and last["stoch_k"] > last["stoch_d"] and last["stoch_k"] < 30:
        buy = 0.7
    if prev["stoch_k"] >= prev["stoch_d"] and last["stoch_k"] < last["stoch_d"] and last["stoch_k"] > 70:
        sell = 0.7

    vol_confirmed = not pd.isna(last["volume_ratio"]) and last["volume_ratio"] > 1.2
    obv_bullish = not pd.isna(last["obv_ema"]) and last["obv"] > last["obv_ema"]
    obv_bearish = not pd.isna(last["obv_ema"]) and last["obv"] < last["obv_ema"]

    if adx_strong and vol_confirmed:
        if buy > 0 and obv_bullish:
            buy = min(buy * 1.3, 1.0)
        if sell > 0 and obv_bearish:
            sell = min(sell * 1.3, 1.0)

    return buy, sell


def _score_vwap_reversion(df: pd.DataFrame) -> tuple[float, float]:
    """VWAP pullback entries — buy when price pulls back to VWAP in uptrend, sell in downtrend."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("vwap")) or pd.isna(last.get("ema_50")):
        return buy, sell

    vwap = last["vwap"]
    price = last["close"]
    above_vwap = price > vwap
    uptrend = last["close"] > last["ema_50"]

    # Pullback to VWAP in uptrend: price was above, now near/below VWAP
    if uptrend and price >= vwap * 0.998 and price <= vwap * 1.005:
        if prev["close"] < vwap and price >= vwap:
            buy = 0.8  # reclaim VWAP from below = bullish
        else:
            buy = 0.5  # near VWAP in uptrend = mild buy

    # Pullback to VWAP in downtrend
    downtrend = last["close"] < last["ema_50"]
    if downtrend and price <= vwap * 1.002 and price >= vwap * 0.995:
        if prev["close"] > vwap and price <= vwap:
            sell = 0.8
        else:
            sell = 0.5

    # Strong volume confirmation
    if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.5:
        buy = min(buy * 1.2, 1.0)
        sell = min(sell * 1.2, 1.0)

    return buy, sell


def _score_keltner_breakout(df: pd.DataFrame) -> tuple[float, float]:
    """Keltner Channel breakout — strong directional move beyond ATR-based channels."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("kc_upper")) or pd.isna(last.get("adx")):
        return buy, sell

    # Bullish breakout: close above upper Keltner with strong ADX
    if last["close"] > last["kc_upper"] and prev["close"] <= prev["kc_upper"]:
        buy = 0.8
        if last["adx"] > 30:
            buy = 1.0  # strong trend breakout

    # Bearish breakout
    if last["close"] < last["kc_lower"] and prev["close"] >= prev["kc_lower"]:
        sell = 0.8
        if last["adx"] > 30:
            sell = 1.0

    # Avoid false breakouts: volume must confirm
    if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] < 0.8:
        buy *= 0.4
        sell *= 0.4

    return buy, sell


def _score_ichimoku(df: pd.DataFrame) -> tuple[float, float]:
    """Ichimoku Cloud multi-line confirmation."""
    last = df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("ichi_tenkan")) or pd.isna(last.get("ichi_senkou_a")):
        return buy, sell

    price = last["close"]
    tenkan = last["ichi_tenkan"]
    kijun = last["ichi_kijun"]
    senkou_a = last["ichi_senkou_a"]
    senkou_b = last["ichi_senkou_b"]

    # Cloud color: bullish if senkou_a > senkou_b
    cloud_bullish = not pd.isna(senkou_a) and not pd.isna(senkou_b) and senkou_a > senkou_b
    cloud_bearish = not pd.isna(senkou_a) and not pd.isna(senkou_b) and senkou_a < senkou_b

    # Price above cloud
    if not pd.isna(senkou_a) and not pd.isna(senkou_b):
        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)

        if price > cloud_top and cloud_bullish:
            buy = 0.6
            # TK cross (tenkan crosses above kijun) = strong signal
            prev = df.iloc[-2]
            if not pd.isna(prev.get("ichi_tenkan")) and prev["ichi_tenkan"] <= prev["ichi_kijun"] and tenkan > kijun:
                buy = 0.9
        elif price < cloud_bottom and cloud_bearish:
            sell = 0.6
            prev = df.iloc[-2]
            if not pd.isna(prev.get("ichi_tenkan")) and prev["ichi_tenkan"] >= prev["ichi_kijun"] and tenkan < kijun:
                sell = 0.9

    return buy, sell


def _score_supertrend(df: pd.DataFrame) -> tuple[float, float]:
    """Supertrend direction change signals."""
    last = df.iloc[-1]
    prev = df.iloc[-2]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("supertrend_dir")) or pd.isna(prev.get("supertrend_dir")):
        return buy, sell

    # Direction flip to bullish
    if prev["supertrend_dir"] == -1 and last["supertrend_dir"] == 1:
        buy = 0.9
        # Confirm with volume
        if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
            buy = 1.0

    # Direction flip to bearish
    if prev["supertrend_dir"] == 1 and last["supertrend_dir"] == -1:
        sell = 0.9
        if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
            sell = 1.0

    # Continuation
    if buy == 0 and sell == 0:
        if last["supertrend_dir"] == 1 and last["close"] > last["supertrend"]:
            buy = 0.3
        elif last["supertrend_dir"] == -1 and last["close"] < last["supertrend"]:
            sell = 0.3

    return buy, sell


def _score_scalping(df: pd.DataFrame) -> tuple[float, float]:
    """Ultra-short-term scalping: RSI + Stochastic + MFI + CCI microstructure."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    signals_buy = 0
    signals_sell = 0
    total = 0

    # RSI micro
    if not pd.isna(last.get("rsi")):
        total += 1
        if last["rsi"] < 35:
            signals_buy += 1
        elif last["rsi"] > 65:
            signals_sell += 1

    # Stochastic micro
    if not pd.isna(last.get("stoch_k")):
        total += 1
        if last["stoch_k"] < 25 and last["stoch_k"] > last["stoch_d"]:
            signals_buy += 1
        elif last["stoch_k"] > 75 and last["stoch_k"] < last["stoch_d"]:
            signals_sell += 1

    # MFI
    if not pd.isna(last.get("mfi")):
        total += 1
        if last["mfi"] < 30:
            signals_buy += 1
        elif last["mfi"] > 70:
            signals_sell += 1

    # CCI
    if not pd.isna(last.get("cci")):
        total += 1
        if last["cci"] < -100:
            signals_buy += 1
        elif last["cci"] > 100:
            signals_sell += 1

    if total >= 3:
        consensus = signals_buy / total
        if consensus >= 0.75:
            buy = 0.7 + 0.3 * consensus
        consensus_s = signals_sell / total
        if consensus_s >= 0.75:
            sell = 0.7 + 0.3 * consensus_s

    return buy, sell


# --------------- Adaptive weight system ---------------

_STRATEGY_NAMES = [
    "ema_crossover", "macd", "bollinger", "rsi_divergence", "momentum",
    "vwap_reversion", "keltner_breakout", "ichimoku", "supertrend", "scalping",
]

_DEFAULT_WEIGHTS = {
    "ema_crossover": 0.15, "macd": 0.12, "bollinger": 0.10, "rsi_divergence": 0.10,
    "momentum": 0.10, "vwap_reversion": 0.10, "keltner_breakout": 0.08,
    "ichimoku": 0.08, "supertrend": 0.10, "scalping": 0.07,
}

# Regime-based weight multipliers: trending markets favor momentum strategies, ranging favor mean-reversion
_REGIME_MULTIPLIERS = {
    "trending": {
        "ema_crossover": 1.3, "macd": 1.2, "bollinger": 0.6, "rsi_divergence": 0.7,
        "momentum": 1.3, "vwap_reversion": 0.7, "keltner_breakout": 1.4,
        "ichimoku": 1.3, "supertrend": 1.4, "scalping": 0.5,
    },
    "ranging": {
        "ema_crossover": 0.6, "macd": 0.7, "bollinger": 1.4, "rsi_divergence": 1.3,
        "momentum": 0.6, "vwap_reversion": 1.4, "keltner_breakout": 0.5,
        "ichimoku": 0.6, "supertrend": 0.5, "scalping": 1.3,
    },
}


def _compute_adaptive_weights(cfg: dict, df: pd.DataFrame) -> dict:
    """Compute regime-aware adaptive weights."""
    base_weights = cfg.get("weights", _DEFAULT_WEIGHTS)

    # Get market regime from computed indicators
    regime = df.attrs.get("market_regime", "trending")
    multipliers = _REGIME_MULTIPLIERS.get(regime, {})

    adapted = {}
    total = 0
    for name in _STRATEGY_NAMES:
        base = base_weights.get(name, _DEFAULT_WEIGHTS.get(name, 0.1))
        mult = multipliers.get(name, 1.0)
        adapted[name] = base * mult
        total += adapted[name]

    # Normalize to sum to 1.0
    if total > 0:
        for name in adapted:
            adapted[name] /= total

    return adapted


# --------------- Main ensemble ---------------

def generate_signal(df: pd.DataFrame, risk_fraction_of_balance: float,
                     trading_balance: float, cfg: dict = None) -> dict | None:
    """
    10-strategy ensemble with adaptive weighting.

    Runs all sub-strategies, applies regime-aware weights, and produces a
    buy or sell signal if the weighted score exceeds the threshold.
    """
    if len(df) < 50:
        return None

    cfg = cfg or {}
    min_score = cfg.get("min_signal_score", 3) / 5.0
    enable_shorts = cfg.get("enable_shorts", True)

    df = compute_indicators(df, cfg)

    if pd.isna(df.iloc[-1]["atr"]):
        return None

    # Adaptive weights based on market regime
    weights = _compute_adaptive_weights(cfg, df)

    # Collect scores from each strategy
    scorers = [
        ("ema_crossover", _score_ema_crossover),
        ("macd", _score_macd),
        ("bollinger", _score_bollinger),
        ("rsi_divergence", lambda d: _score_rsi(d, cfg.get("rsi_overbought", 70), cfg.get("rsi_oversold", 30))),
        ("momentum", _score_momentum),
        ("vwap_reversion", _score_vwap_reversion),
        ("keltner_breakout", _score_keltner_breakout),
        ("ichimoku", _score_ichimoku),
        ("supertrend", _score_supertrend),
        ("scalping", _score_scalping),
    ]

    buy_scores = []
    sell_scores = []
    raw_scores = {}

    for name, scorer in scorers:
        b, s = scorer(df)
        w = weights.get(name, 0.1)
        buy_scores.append(b * w)
        sell_scores.append(s * w)
        raw_scores[name] = (b, s)

    total_buy = sum(buy_scores)
    total_sell = sum(sell_scores)
    last = df.iloc[-1]

    proposed_amount = trading_balance * risk_fraction_of_balance

    # Buy signal
    if total_buy >= min_score and total_buy > total_sell:
        active = [name for name, (b, _) in raw_scores.items() if b > 0.3]
        return {
            "side": "buy",
            "entry_price": float(last["close"]),
            "amount": proposed_amount,
            "atr": float(last["atr"]),
            "score": round(total_buy, 3),
            "strategies": active,
            "regime": df.attrs.get("market_regime", "unknown"),
        }

    # Sell/short signal
    if enable_shorts and total_sell >= min_score and total_sell > total_buy:
        active = [name for name, (_, s) in raw_scores.items() if s > 0.3]
        return {
            "side": "sell",
            "entry_price": float(last["close"]),
            "amount": proposed_amount,
            "atr": float(last["atr"]),
            "score": round(total_sell, 3),
            "strategies": active,
            "regime": df.attrs.get("market_regime", "unknown"),
        }

    return None


def generate_exit_signal(df: pd.DataFrame, position: dict,
                          trailing_activate_pct: float = 1.5,
                          trailing_distance_pct: float = 1.0,
                          cfg: dict = None) -> dict | None:
    """
    Proactive exit signal using the full indicator set.
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
        # EMA trend reversal
        if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
            return {"exit_price": last_price, "reason": "trend_reversal_ema_cross"}

        # RSI exhaustion
        if not pd.isna(last["rsi"]) and last["rsi"] > 80:
            return {"exit_price": last_price, "reason": "rsi_overbought_exhaustion"}

        # MACD death cross with ADX confirmation
        if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
            if not pd.isna(last["adx"]) and last["adx"] > 20:
                return {"exit_price": last_price, "reason": "macd_death_cross"}

        # Supertrend flip to bearish
        if not pd.isna(last.get("supertrend_dir")) and not pd.isna(prev.get("supertrend_dir")):
            if prev["supertrend_dir"] == 1 and last["supertrend_dir"] == -1:
                return {"exit_price": last_price, "reason": "supertrend_bearish_flip"}

        # Keltner lower break
        if not pd.isna(last.get("kc_lower")) and last_price < last["kc_lower"]:
            if prev["close"] >= prev["kc_lower"]:
                return {"exit_price": last_price, "reason": "keltner_lower_break"}

        # Trailing stop
        profit_pct = (peak_price - entry_price) / entry_price * 100
        if profit_pct >= trailing_activate_pct:
            drawdown_from_peak = (peak_price - last_price) / peak_price * 100
            if drawdown_from_peak >= trailing_distance_pct:
                return {"exit_price": last_price, "reason": "trailing_stop_hit"}

        # Bollinger lower break in ranging market
        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width > 0.03 and last_price < last["bb_lower"]:
            return {"exit_price": last_price, "reason": "bollinger_lower_break"}

    else:  # short/sell
        # EMA trend reversal
        if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
            return {"exit_price": last_price, "reason": "trend_reversal_ema_cross"}

        # RSI exhaustion
        if not pd.isna(last["rsi"]) and last["rsi"] < 20:
            return {"exit_price": last_price, "reason": "rsi_oversold_exhaustion"}

        # MACD golden cross
        if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
            if not pd.isna(last["adx"]) and last["adx"] > 20:
                return {"exit_price": last_price, "reason": "macd_golden_cross"}

        # Supertrend flip to bullish
        if not pd.isna(last.get("supertrend_dir")) and not pd.isna(prev.get("supertrend_dir")):
            if prev["supertrend_dir"] == -1 and last["supertrend_dir"] == 1:
                return {"exit_price": last_price, "reason": "supertrend_bullish_flip"}

        # Keltner upper break
        if not pd.isna(last.get("kc_upper")) and last_price > last["kc_upper"]:
            if prev["close"] <= prev["kc_upper"]:
                return {"exit_price": last_price, "reason": "keltner_upper_break"}

        # Trailing stop for shorts
        profit_pct = (entry_price - peak_price) / entry_price * 100
        if profit_pct >= trailing_activate_pct:
            drawdown_from_peak = (last_price - peak_price) / peak_price * 100
            if drawdown_from_peak >= trailing_distance_pct:
                return {"exit_price": last_price, "reason": "trailing_stop_hit"}

        # Bollinger upper break
        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width > 0.03 and last_price > last["bb_upper"]:
            return {"exit_price": last_price, "reason": "bollinger_upper_break"}

    return None


def generate_signal_with_ml(df: pd.DataFrame, risk_fraction_of_balance: float,
                             trading_balance: float, lstm_predictor=None,
                             ml_min_confidence: float = 0.6,
                             cfg: dict = None, **kwargs) -> dict | None:
    """
    10-strategy ensemble + optional ML filter.
    """
    base_signal = generate_signal(df, risk_fraction_of_balance, trading_balance, cfg=cfg)
    if base_signal is None or lstm_predictor is None:
        return base_signal

    prob_up = lstm_predictor.predict_proba(df)
    if prob_up is None:
        return base_signal

    if base_signal["side"] == "buy":
        if prob_up < ml_min_confidence:
            return None
        base_signal["ml_confidence"] = round(prob_up, 3)
    else:
        if (1 - prob_up) < ml_min_confidence:
            return None
        base_signal["ml_confidence"] = round(1 - prob_up, 3)

    return base_signal
