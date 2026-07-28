"""
Multi-strategy ensemble covering all 10 trading strategy categories.

Single-Symbol Strategies (run per-symbol on OHLCV data):
  TREND-FOLLOWING:
   1. EMA Crossover - EMA(9)/EMA(21) with EMA(50) filter
   2. MACD - Histogram zero-cross, signal-line cross, divergence
   3. Ichimoku Cloud - Multi-line trend system
   4. Supertrend - ATR-based trend following
   5. Donchian Channel - N-period high/low breakout (turtle trading)
   6. ADX Filtered - Only trade when ADX > threshold + directional DI

  MEAN REVERSION:
   7. Bollinger Bands - Band reversion + squeeze detection
   8. RSI Divergence - Oversold/overbought bounces + divergence
   9. VWAP Reversion - VWAP pullback entries
  10. Z-Score Reversion - Statistical z-score vs rolling mean

  MOMENTUM:
  11. Multi-Factor Momentum - ADX + Stochastic + Volume + OBV

  BREAKOUT:
  12. Keltner Channel Breakout - ATR-based channel breakout
  13. Opening Range Breakout - First N bars define range, trade the breakout
  14. ATR Volatility Breakout - ATR expansion triggers entry

  MARKET MAKING:
  15. Spread Capture - Quote near support/resistance, earn the spread

  SCALPING:
  16. Microstructure Scalping - RSI + Stochastic + MFI + CCI consensus

  NEWS/SENTIMENT (single-symbol, uses cached sentiment):
  17. Sentiment Boost - NLP sentiment score adjusts existing signals

  ML/STATISTICAL:
  18. ML Prediction - LSTM probability as standalone strategy scorer
  19. Volatility Forecast - GARCH-lite vol prediction sizes positions

Multi-Symbol Portfolio Strategies (run across all symbols):
  20. Cross-Exchange Arbitrage - Price divergence across exchanges
  21. Triangular Arbitrage - Three-pair rate inconsistency
  22. Risk-Parity Rotation - Equal risk contribution across asset classes
  23. DCA Timing - Dollar-cost averaging with volatility adjustment
  24. Safe-Haven Rotation - Rotate into gold during equity/crypto drawdowns
  25. Options Signals - Covered call / straddle opportunity detection

Signal contract expected by main.py / execution_manager:
    {"side": "buy"|"sell", "entry_price": float, "amount": float,
     "atr": float, "score": float, "strategies": list[str],
     "regime": str}
"""
import math
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


def _plus_di(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    plus_dm = high.diff()
    minus_dm = -df["low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    tr = pd.concat([
        high - df["low"],
        (high - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))


def _minus_di(df: pd.DataFrame, period: int = 14) -> pd.Series:
    low = df["low"]
    plus_dm = df["high"].diff()
    minus_dm = -low.diff()
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([
        df["high"] - low,
        (df["high"] - df["close"].shift(1)).abs(),
        (low - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))


def _stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def _vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cumvol = df["volume"].cumsum()
    cumtpv = (typical * df["volume"]).cumsum()
    return cumtpv / cumvol.replace(0, np.nan)


def _rolling_vwap(df: pd.DataFrame, period: int = 20) -> pd.Series:
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
    high, low = df["high"], df["low"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    chikou = df["close"].shift(-26)
    return tenkan, kijun, senkou_a, senkou_b, chikou


def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
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
    typical = (df["high"] + df["low"] + df["close"]) / 3
    raw_mf = typical * df["volume"]
    pos_mf = raw_mf.where(typical > typical.shift(1), 0).rolling(period).sum()
    neg_mf = raw_mf.where(typical < typical.shift(1), 0).rolling(period).sum()
    mfr = pos_mf / neg_mf.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))


def _cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    sma = typical.rolling(period).mean()
    mad = typical.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (typical - sma) / (0.015 * mad).replace(0, np.nan)


def _donchian(df: pd.DataFrame, period: int = 20):
    upper = df["high"].rolling(period).max()
    lower = df["low"].rolling(period).min()
    mid = (upper + lower) / 2
    return upper, mid, lower


def _zscore(series: pd.Series, period: int = 20) -> pd.Series:
    mean = series.rolling(period).mean()
    std = series.rolling(period).std()
    return (series - mean) / std.replace(0, np.nan)


def _garch_lite(df: pd.DataFrame, period: int = 20, forecast_period: int = 10) -> pd.Series:
    """Simplified GARCH(1,1)-like volatility forecast using EWMA variance."""
    log_returns = np.log(df["close"] / df["close"].shift(1))
    squared = log_returns ** 2
    alpha = 0.06
    beta = 0.90
    var_series = squared.ewm(alpha=alpha).mean()
    for _ in range(forecast_period):
        var_series = alpha * squared + beta * var_series
    return np.sqrt(var_series) * 100


def _opening_range(df: pd.DataFrame, period: int = 20):
    """Opening range: high/low of first N bars, projected forward."""
    range_high = df["high"].rolling(period).max()
    range_low = df["low"].rolling(period).min()
    range_mid = (range_high + range_low) / 2
    return range_high, range_low, range_mid


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
    df["sma_200"] = _sma(df["close"], 200)

    # Momentum
    df["rsi"] = _rsi(df["close"], rsi_period)
    df["macd"], df["macd_signal"], df["macd_hist"] = _macd(df["close"])
    df["adx"] = _adx(df)
    df["plus_di"] = _plus_di(df)
    df["minus_di"] = _minus_di(df)
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

    # Donchian Channels
    donchian_period = cfg.get("donchian_period", 20)
    df["dc_upper"], df["dc_mid"], df["dc_lower"] = _donchian(df, donchian_period)

    # Z-Score
    df["zscore"] = _zscore(df["close"], cfg.get("zscore_period", 20))

    # GARCH-lite volatility forecast
    df["vol_forecast"] = _garch_lite(df, 20, cfg.get("vol_forecast_horizon", 10))
    df["vol_pct"] = df["atr"] / df["close"] * 100

    # Opening Range
    or_period = cfg.get("opening_range_period", 20)
    df["or_high"], df["or_low"], df["or_mid"] = _opening_range(df, or_period)

    # ATR expansion detection
    df["atr_sma"] = _sma(df["atr"], 20)
    df["atr_expansion"] = df["atr"] / df["atr_sma"].replace(0, np.nan)

    # Multi-timeframe helpers
    df["ema_200_slope"] = df["ema_200"].pct_change(5) * 100

    # Detect market regime: trending vs ranging
    adx_vals = df["adx"].iloc[-5:]
    avg_adx = adx_vals.mean() if not adx_vals.isna().all() else 20
    bb_width = (df["bb_upper"].iloc[-1] - df["bb_lower"].iloc[-1]) / df["bb_mid"].iloc[-1] if df["bb_mid"].iloc[-1] > 0 else 0
    df.attrs["market_regime"] = "trending" if avg_adx > 25 else "ranging"
    df.attrs["bb_width"] = bb_width

    return df


# --------------- Strategy scorers ---------------
# Each returns (buy_score, sell_score) in range [-1, 1].

# --- 1. EMA Crossover (Trend-Following) ---
def _score_ema_crossover(df: pd.DataFrame) -> tuple[float, float]:
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


# --- 2. MACD (Trend-Following) ---
def _score_macd(df: pd.DataFrame) -> tuple[float, float]:
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


# --- 3. Ichimoku (Trend-Following) ---
def _score_ichimoku(df: pd.DataFrame) -> tuple[float, float]:
    last = df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("ichi_tenkan")) or pd.isna(last.get("ichi_senkou_a")):
        return buy, sell

    price = last["close"]
    tenkan = last["ichi_tenkan"]
    kijun = last["ichi_kijun"]
    senkou_a = last["ichi_senkou_a"]
    senkou_b = last["ichi_senkou_b"]

    cloud_bullish = not pd.isna(senkou_a) and not pd.isna(senkou_b) and senkou_a > senkou_b
    cloud_bearish = not pd.isna(senkou_a) and not pd.isna(senkou_b) and senkou_a < senkou_b

    if not pd.isna(senkou_a) and not pd.isna(senkou_b):
        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)

        if price > cloud_top and cloud_bullish:
            buy = 0.6
            prev = df.iloc[-2]
            if not pd.isna(prev.get("ichi_tenkan")) and prev["ichi_tenkan"] <= prev["ichi_kijun"] and tenkan > kijun:
                buy = 0.9
        elif price < cloud_bottom and cloud_bearish:
            sell = 0.6
            prev = df.iloc[-2]
            if not pd.isna(prev.get("ichi_tenkan")) and prev["ichi_tenkan"] >= prev["ichi_kijun"] and tenkan < kijun:
                sell = 0.9

    return buy, sell


# --- 4. Supertrend (Trend-Following) ---
def _score_supertrend(df: pd.DataFrame) -> tuple[float, float]:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("supertrend_dir")) or pd.isna(prev.get("supertrend_dir")):
        return buy, sell

    if prev["supertrend_dir"] == -1 and last["supertrend_dir"] == 1:
        buy = 0.9
        if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
            buy = 1.0

    if prev["supertrend_dir"] == 1 and last["supertrend_dir"] == -1:
        sell = 0.9
        if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
            sell = 1.0

    if buy == 0 and sell == 0:
        if last["supertrend_dir"] == 1 and last["close"] > last["supertrend"]:
            buy = 0.3
        elif last["supertrend_dir"] == -1 and last["close"] < last["supertrend"]:
            sell = 0.3

    return buy, sell


# --- 5. Donchian Channel (Trend-Following / Turtle Trading) ---
def _score_donchian(df: pd.DataFrame) -> tuple[float, float]:
    """Donchian channel breakout — classic turtle trading entry."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("dc_upper")) or pd.isna(prev.get("dc_upper")):
        return buy, sell

    # Bullish breakout: price breaks above upper Donchian channel
    if last["close"] > prev["dc_upper"] and prev["close"] <= prev["dc_upper"]:
        buy = 0.9
        # Volume + ADX confirmation for high-confidence
        adx_ok = not pd.isna(last.get("adx")) and last["adx"] > 25
        vol_ok = not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.2
        if adx_ok and vol_ok:
            buy = 1.0
        elif adx_ok or vol_ok:
            buy = 0.85

    # Bearish breakout: price breaks below lower Donchian channel
    if last["close"] < prev["dc_lower"] and prev["close"] >= prev["dc_lower"]:
        sell = 0.9
        adx_ok = not pd.isna(last.get("adx")) and last["adx"] > 25
        vol_ok = not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.2
        if adx_ok and vol_ok:
            sell = 1.0
        elif adx_ok or vol_ok:
            sell = 0.85

    # Continuation: price holding near upper band in uptrend
    if buy == 0 and sell == 0:
        if last["close"] > last["dc_mid"] and last["close"] > last.get("ema_50", last["close"]):
            buy = 0.2
        elif last["close"] < last["dc_mid"] and last["close"] < last.get("ema_50", last["close"]):
            sell = 0.2

    return buy, sell


# --- 6. ADX Filtered (Trend-Following) ---
def _score_adx_filtered(df: pd.DataFrame, adx_threshold: float = 25) -> tuple[float, float]:
    """ADX-filtered trend entries: only trade when ADX > threshold + directional DI confirms."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("adx")) or pd.isna(last.get("plus_di")):
        return buy, sell

    adx_strong = last["adx"] > adx_threshold
    if not adx_strong:
        return buy, sell

    # DI crossover + strong ADX = high-confidence trend entry
    if prev["plus_di"] <= prev["minus_di"] and last["plus_di"] > last["minus_di"]:
        buy = 0.8
        if last["adx"] > 35:
            buy = 1.0
        if last["close"] > last.get("ema_50", last["close"]):
            buy = min(buy + 0.1, 1.0)

    if prev["plus_di"] >= prev["minus_di"] and last["plus_di"] < last["minus_di"]:
        sell = 0.8
        if last["adx"] > 35:
            sell = 1.0
        if last["close"] < last.get("ema_50", last["close"]):
            sell = min(sell + 0.1, 1.0)

    # Strong trend continuation
    if buy == 0 and sell == 0:
        if last["plus_di"] > last["minus_di"] and last["adx"] > 30:
            buy = 0.4
        elif last["minus_di"] > last["plus_di"] and last["adx"] > 30:
            sell = 0.4

    return buy, sell


# --- 7. Bollinger Bands (Mean Reversion) ---
def _score_bollinger(df: pd.DataFrame) -> tuple[float, float]:
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


# --- 8. RSI Divergence (Mean Reversion) ---
def _score_rsi(df: pd.DataFrame, overbought: int = 70, oversold: int = 30) -> tuple[float, float]:
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


# --- 9. VWAP Reversion (Mean Reversion) ---
def _score_vwap_reversion(df: pd.DataFrame) -> tuple[float, float]:
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("vwap")) or pd.isna(last.get("ema_50")):
        return buy, sell

    vwap = last["vwap"]
    price = last["close"]
    uptrend = last["close"] > last["ema_50"]

    if uptrend and price >= vwap * 0.998 and price <= vwap * 1.005:
        if prev["close"] < vwap and price >= vwap:
            buy = 0.8
        else:
            buy = 0.5

    downtrend = last["close"] < last["ema_50"]
    if downtrend and price <= vwap * 1.002 and price >= vwap * 0.995:
        if prev["close"] > vwap and price <= vwap:
            sell = 0.8
        else:
            sell = 0.5

    if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.5:
        buy = min(buy * 1.2, 1.0)
        sell = min(sell * 1.2, 1.0)

    return buy, sell


# --- 10. Z-Score Reversion (Mean Reversion) ---
def _score_zscore_reversion(df: pd.DataFrame, entry_threshold: float = 2.0,
                             exit_threshold: float = 0.5) -> tuple[float, float]:
    """Z-score based mean reversion: buy when z < -entry_threshold, sell when z > entry_threshold."""
    last = df.iloc[-1]
    prev = df.iloc[-2]
    buy, sell = 0.0, 0.0

    z = last.get("zscore")
    if pd.isna(z):
        return buy, sell

    # Strong reversion signal: z-score extreme
    if z < -entry_threshold:
        buy = 0.8
        if z < -2.5:
            buy = 1.0  # extreme oversold
    elif z < -1.5:
        buy = 0.4

    if z > entry_threshold:
        sell = 0.8
        if z > 2.5:
            sell = 1.0  # extreme overbought
    elif z > 1.5:
        sell = 0.4

    # Mean reversion confirmation: z-score returning toward zero
    prev_z = prev.get("zscore")
    if not pd.isna(prev_z):
        if prev_z < -entry_threshold and z > prev_z and z < -exit_threshold:
            buy = max(buy, 0.6)  # z recovering from extreme
        if prev_z > entry_threshold and z < prev_z and z > exit_threshold:
            sell = max(sell, 0.6)

    # Volume confirmation
    if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
        buy = min(buy * 1.15, 1.0)
        sell = min(sell * 1.15, 1.0)

    return buy, sell


# --- 11. Multi-Factor Momentum (Momentum) ---
def _score_momentum(df: pd.DataFrame) -> tuple[float, float]:
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    adx_strong = not pd.isna(last["adx"]) and last["adx"] > 25

    if prev["stoch_k"] <= prev["stoch_d"] and last["stoch_k"] > last["stoch_d"] and last["stoch_k"] < 30:
        buy = 0.7
    if prev["stoch_k"] >= prev["stoch_d"] and last["stoch_k"] < last["stoch_d"] and last["stoch_k"] > 70:
        sell = 0.7

    vol_confirmed = not pd.isna(last["volume_ratio"]) and last["volume_ratio"] > 1.2
    obv_bullish = not pd.isna(last.get("obv_ema")) and last["obv"] > last["obv_ema"]
    obv_bearish = not pd.isna(last.get("obv_ema")) and last["obv"] < last["obv_ema"]

    if adx_strong and vol_confirmed:
        if buy > 0 and obv_bullish:
            buy = min(buy * 1.3, 1.0)
        if sell > 0 and obv_bearish:
            sell = min(sell * 1.3, 1.0)

    return buy, sell


# --- 12. Keltner Channel Breakout (Breakout) ---
def _score_keltner_breakout(df: pd.DataFrame) -> tuple[float, float]:
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("kc_upper")) or pd.isna(last.get("adx")):
        return buy, sell

    if last["close"] > last["kc_upper"] and prev["close"] <= prev["kc_upper"]:
        buy = 0.8
        if last["adx"] > 30:
            buy = 1.0

    if last["close"] < last["kc_lower"] and prev["close"] >= prev["kc_lower"]:
        sell = 0.8
        if last["adx"] > 30:
            sell = 1.0

    if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] < 0.8:
        buy *= 0.4
        sell *= 0.4

    return buy, sell


# --- 13. Opening Range Breakout (Breakout) ---
def _score_opening_range_breakout(df: pd.DataFrame) -> tuple[float, float]:
    """Opening range breakout: enter when price breaks the first N-bar range."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("or_high")) or pd.isna(last.get("or_low")):
        return buy, sell

    or_high = last["or_high"]
    or_low = last["or_low"]
    or_range = or_high - or_low

    if or_range <= 0:
        return buy, sell

    # Bullish breakout above opening range
    if last["close"] > or_high and prev["close"] <= or_high:
        buy = 0.8
        # Strong confirmation: high volume + price well above range
        breakout_magnitude = (last["close"] - or_high) / or_range
        if breakout_magnitude > 0.5 and not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
            buy = 1.0
        elif breakout_magnitude > 0.3:
            buy = 0.9

    # Bearish breakout below opening range
    if last["close"] < or_low and prev["close"] >= or_low:
        sell = 0.8
        breakout_magnitude = (or_low - last["close"]) / or_range
        if breakout_magnitude > 0.5 and not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.3:
            sell = 1.0
        elif breakout_magnitude > 0.3:
            sell = 0.9

    # Avoid false breakouts: need ADX or volume confirmation
    if buy > 0 or sell > 0:
        adx_ok = not pd.isna(last.get("adx")) and last["adx"] > 20
        vol_ok = not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.0
        if not adx_ok and not vol_ok:
            buy *= 0.5
            sell *= 0.5

    return buy, sell


# --- 14. ATR Volatility Breakout (Breakout) ---
def _score_atr_volatility_breakout(df: pd.DataFrame, expansion_threshold: float = 1.5) -> tuple[float, float]:
    """ATR expansion triggers entry: volatility is expanding = breakout is real."""
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    atr_exp = last.get("atr_expansion")
    if pd.isna(atr_exp):
        return buy, sell

    expanding = atr_exp > expansion_threshold
    if not expanding:
        return buy, sell

    # Price direction combined with volatility expansion
    price_up = last["close"] > prev["close"]
    price_down = last["close"] < prev["close"]

    if price_up and expanding:
        buy = 0.7
        if atr_exp > 2.0:
            buy = 0.9
        # Strong trend alignment
        if not pd.isna(last.get("adx")) and last["adx"] > 30:
            buy = min(buy + 0.1, 1.0)

    if price_down and expanding:
        sell = 0.7
        if atr_exp > 2.0:
            sell = 0.9
        if not pd.isna(last.get("adx")) and last["adx"] > 30:
            sell = min(sell + 0.1, 1.0)

    # Volume must confirm real breakout
    if not pd.isna(last.get("volume_ratio")) and last["volume_ratio"] > 1.5:
        buy = min(buy * 1.2, 1.0)
        sell = min(sell * 1.2, 1.0)

    return buy, sell


# --- 15. Spread Capture / Market Making ---
def _score_spread_capture(df: pd.DataFrame) -> tuple[float, float]:
    """Market making: identify tight range + low volatility for spread capture."""
    last = df.iloc[-1]
    buy, sell = 0.0, 0.0

    if pd.isna(last.get("bb_upper")) or pd.isna(last.get("atr")):
        return buy, sell

    bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
    atr_pct = last["atr"] / last["close"] * 100 if last["close"] > 0 else 0

    # Low volatility = good for spread capture
    low_vol = bb_width < 0.03 and atr_pct < 2.0

    if not low_vol:
        return buy, sell

    # Price near Bollinger mid = neutral, good for quote-based entry
    price_position = (last["close"] - last["bb_lower"]) / (last["bb_upper"] - last["bb_lower"]) \
        if (last["bb_upper"] - last["bb_lower"]) > 0 else 0.5

    # Near mid = good for market making both sides
    if 0.4 <= price_position <= 0.6:
        buy = 0.5  # buy at lower quote
        sell = 0.5  # sell at upper quote

    # Tight range + RSI neutral = mean-reverting, ideal for spread capture
    if not pd.isna(last.get("rsi")) and 40 < last["rsi"] < 60:
        buy = max(buy, 0.4)
        sell = max(sell, 0.4)

    return buy, sell


# --- 16. Scalping (Microstructure) ---
def _score_scalping(df: pd.DataFrame) -> tuple[float, float]:
    prev, last = df.iloc[-2], df.iloc[-1]
    buy, sell = 0.0, 0.0

    signals_buy = 0
    signals_sell = 0
    total = 0

    if not pd.isna(last.get("rsi")):
        total += 1
        if last["rsi"] < 35:
            signals_buy += 1
        elif last["rsi"] > 65:
            signals_sell += 1

    if not pd.isna(last.get("stoch_k")):
        total += 1
        if last["stoch_k"] < 25 and last["stoch_k"] > last["stoch_d"]:
            signals_buy += 1
        elif last["stoch_k"] > 75 and last["stoch_k"] < last["stoch_d"]:
            signals_sell += 1

    if not pd.isna(last.get("mfi")):
        total += 1
        if last["mfi"] < 30:
            signals_buy += 1
        elif last["mfi"] > 70:
            signals_sell += 1

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


# --- 17. Sentiment Boost (News/Sentiment) ---
def _score_sentiment(df: pd.DataFrame, sentiment_score: float = None) -> tuple[float, float]:
    """Sentiment-driven signal adjustment. sentiment_score: -1 (bearish) to +1 (bullish)."""
    buy, sell = 0.0, 0.0

    if sentiment_score is None or pd.isna(sentiment_score):
        return buy, sell

    # Strong bullish sentiment
    if sentiment_score > 0.5:
        buy = 0.7
        if sentiment_score > 0.8:
            buy = 0.9
    elif sentiment_score > 0.2:
        buy = 0.3

    # Strong bearish sentiment
    if sentiment_score < -0.5:
        sell = 0.7
        if sentiment_score < -0.8:
            sell = 0.9
    elif sentiment_score < -0.2:
        sell = 0.3

    return buy, sell


# --- 18. ML Prediction as Strategy (ML/Statistical) ---
def _score_ml_prediction(df: pd.DataFrame, ml_probability: float = None,
                         confidence_threshold: float = 0.6) -> tuple[float, float]:
    """LSTM probability as a standalone strategy scorer."""
    buy, sell = 0.0, 0.0

    if ml_probability is None or pd.isna(ml_probability):
        return buy, sell

    if ml_probability > confidence_threshold:
        # Map probability to score: 0.6 -> 0.3, 0.8 -> 0.7, 0.95 -> 1.0
        buy = min((ml_probability - confidence_threshold) / (1.0 - confidence_threshold), 1.0)
        buy = 0.3 + buy * 0.7

    inv_prob = 1.0 - ml_probability
    if inv_prob > confidence_threshold:
        sell = min((inv_prob - confidence_threshold) / (1.0 - confidence_threshold), 1.0)
        sell = 0.3 + sell * 0.7

    return buy, sell


# --- 19. Volatility Forecast (ML/Statistical) ---
def _score_volatility_forecast(df: pd.DataFrame) -> tuple[float, float]:
    """GARCH-lite volatility forecast: high predicted vol = avoid/reduce, low = opportunity."""
    last = df.iloc[-1]
    buy, sell = 0.0, 0.0

    vol_fc = last.get("vol_forecast")
    vol_pct = last.get("vol_pct")
    if pd.isna(vol_fc) or pd.isna(vol_pct):
        return buy, sell

    # Volatility expansion expected = caution (reduce position, potential breakout)
    vol_ratio = vol_fc / vol_pct if vol_pct > 0 else 1.0

    if vol_ratio > 1.5:
        # High vol expected — signal breakout direction if trending
        if not pd.isna(last.get("adx")) and last["adx"] > 25:
            if last.get("plus_di", 0) > last.get("minus_di", 0):
                buy = 0.4
            else:
                sell = 0.4
    elif vol_ratio < 0.7:
        # Vol contraction expected — good for mean reversion / spread capture
        rsi = last.get("rsi", 50)
        if not pd.isna(rsi):
            if rsi < 40:
                buy = 0.3
            elif rsi > 60:
                sell = 0.3

    return buy, sell


# --------------- Adaptive weight system ---------------

_STRATEGY_NAMES = [
    # Trend-Following
    "ema_crossover", "macd", "ichimoku", "supertrend", "donchian", "adx_filtered",
    # Mean Reversion
    "bollinger", "rsi_divergence", "vwap_reversion", "zscore_reversion",
    # Momentum
    "momentum",
    # Breakout
    "keltner_breakout", "opening_range_breakout", "atr_volatility_breakout",
    # Market Making
    "spread_capture",
    # Scalping
    "scalping",
    # News/Sentiment
    "sentiment",
    # ML/Statistical
    "ml_prediction", "volatility_forecast",
]

_DEFAULT_WEIGHTS = {
    # Trend-Following (total ~0.35)
    "ema_crossover": 0.08, "macd": 0.07, "ichimoku": 0.06,
    "supertrend": 0.07, "donchian": 0.04, "adx_filtered": 0.03,
    # Mean Reversion (total ~0.18)
    "bollinger": 0.05, "rsi_divergence": 0.05,
    "vwap_reversion": 0.04, "zscore_reversion": 0.04,
    # Momentum (total ~0.07)
    "momentum": 0.07,
    # Breakout (total ~0.13)
    "keltner_breakout": 0.05, "opening_range_breakout": 0.04,
    "atr_volatility_breakout": 0.04,
    # Market Making (total ~0.03)
    "spread_capture": 0.03,
    # Scalping (total ~0.03)
    "scalping": 0.03,
    # News/Sentiment (total ~0.06)
    "sentiment": 0.06,
    # ML/Statistical (total ~0.15)
    "ml_prediction": 0.10, "volatility_forecast": 0.05,
}

_REGIME_MULTIPLIERS = {
    "trending": {
        "ema_crossover": 1.4, "macd": 1.3, "ichimoku": 1.3, "supertrend": 1.5,
        "donchian": 1.5, "adx_filtered": 1.4,
        "bollinger": 0.5, "rsi_divergence": 0.6, "vwap_reversion": 0.6, "zscore_reversion": 0.5,
        "momentum": 1.3,
        "keltner_breakout": 1.4, "opening_range_breakout": 1.3, "atr_volatility_breakout": 1.3,
        "spread_capture": 0.3, "scalping": 0.5,
        "sentiment": 1.2, "ml_prediction": 1.1, "volatility_forecast": 0.8,
    },
    "ranging": {
        "ema_crossover": 0.5, "macd": 0.6, "ichimoku": 0.5, "supertrend": 0.4,
        "donchian": 0.4, "adx_filtered": 0.3,
        "bollinger": 1.5, "rsi_divergence": 1.4, "vwap_reversion": 1.4, "zscore_reversion": 1.5,
        "momentum": 0.5,
        "keltner_breakout": 0.4, "opening_range_breakout": 0.5, "atr_volatility_breakout": 0.6,
        "spread_capture": 1.5, "scalping": 1.4,
        "sentiment": 1.0, "ml_prediction": 1.0, "volatility_forecast": 1.3,
    },
}


def _compute_adaptive_weights(cfg: dict, df: pd.DataFrame) -> dict:
    """Compute regime-aware adaptive weights."""
    base_weights = cfg.get("weights", _DEFAULT_WEIGHTS)

    regime = df.attrs.get("market_regime", "trending")
    multipliers = _REGIME_MULTIPLIERS.get(regime, {})

    adapted = {}
    total = 0
    for name in _STRATEGY_NAMES:
        base = base_weights.get(name, _DEFAULT_WEIGHTS.get(name, 0.05))
        mult = multipliers.get(name, 1.0)
        adapted[name] = base * mult
        total += adapted[name]

    if total > 0:
        for name in adapted:
            adapted[name] /= total

    return adapted


# --------------- Main ensemble (single-symbol) ---------------

def generate_signal(df: pd.DataFrame, risk_fraction_of_balance: float,
                     trading_balance: float, cfg: dict = None,
                     sentiment_score: float = None,
                     ml_probability: float = None) -> dict | None:
    """
    Full strategy ensemble with adaptive weighting.

    Runs all single-symbol sub-strategies, applies regime-aware weights, and
    produces a buy or sell signal if the weighted score exceeds the threshold.

    Optional inputs:
        sentiment_score: float (-1 to 1) from news/social NLP
        ml_probability: float (0 to 1) from LSTM model
    """
    if len(df) < 50:
        return None

    cfg = cfg or {}
    min_score = cfg.get("min_signal_score", 3) / 5.0
    enable_shorts = cfg.get("enable_shorts", True)

    df = compute_indicators(df, cfg)

    if pd.isna(df.iloc[-1]["atr"]):
        return None

    weights = _compute_adaptive_weights(cfg, df)

    scorers = [
        ("ema_crossover", _score_ema_crossover),
        ("macd", _score_macd),
        ("ichimoku", _score_ichimoku),
        ("supertrend", _score_supertrend),
        ("donchian", _score_donchian),
        ("adx_filtered", _score_adx_filtered),
        ("bollinger", _score_bollinger),
        ("rsi_divergence", lambda d: _score_rsi(d, cfg.get("rsi_overbought", 70), cfg.get("rsi_oversold", 30))),
        ("vwap_reversion", _score_vwap_reversion),
        ("zscore_reversion", _score_zscore_reversion),
        ("momentum", _score_momentum),
        ("keltner_breakout", _score_keltner_breakout),
        ("opening_range_breakout", _score_opening_range_breakout),
        ("atr_volatility_breakout", _score_atr_volatility_breakout),
        ("spread_capture", _score_spread_capture),
        ("scalping", _score_scalping),
        ("sentiment", lambda d: _score_sentiment(d, sentiment_score)),
        ("ml_prediction", lambda d: _score_ml_prediction(d, ml_probability, cfg.get("ml_min_confidence", 0.6))),
        ("volatility_forecast", _score_volatility_forecast),
    ]

    buy_scores = []
    sell_scores = []
    raw_scores = {}

    for name, scorer in scorers:
        b, s = scorer(df)
        w = weights.get(name, 0.05)
        buy_scores.append(b * w)
        sell_scores.append(s * w)
        raw_scores[name] = (b, s)

    total_buy = sum(buy_scores)
    total_sell = sum(sell_scores)
    last = df.iloc[-1]

    proposed_amount = trading_balance * risk_fraction_of_balance

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
    """Proactive exit signal using the full indicator set."""
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
        if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
            return {"exit_price": last_price, "reason": "trend_reversal_ema_cross"}

        if not pd.isna(last["rsi"]) and last["rsi"] > 80:
            return {"exit_price": last_price, "reason": "rsi_overbought_exhaustion"}

        if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
            if not pd.isna(last["adx"]) and last["adx"] > 20:
                return {"exit_price": last_price, "reason": "macd_death_cross"}

        if not pd.isna(last.get("supertrend_dir")) and not pd.isna(prev.get("supertrend_dir")):
            if prev["supertrend_dir"] == 1 and last["supertrend_dir"] == -1:
                return {"exit_price": last_price, "reason": "supertrend_bearish_flip"}

        if not pd.isna(last.get("kc_lower")) and last_price < last["kc_lower"]:
            if prev["close"] >= prev["kc_lower"]:
                return {"exit_price": last_price, "reason": "keltner_lower_break"}

        # Donchian lower break exit
        if not pd.isna(last.get("dc_lower")) and last_price < last["dc_lower"]:
            if prev["close"] >= prev["dc_lower"]:
                return {"exit_price": last_price, "reason": "donchian_lower_break"}

        profit_pct = (peak_price - entry_price) / entry_price * 100
        if profit_pct >= trailing_activate_pct:
            drawdown_from_peak = (peak_price - last_price) / peak_price * 100
            if drawdown_from_peak >= trailing_distance_pct:
                return {"exit_price": last_price, "reason": "trailing_stop_hit"}

        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width > 0.03 and last_price < last["bb_lower"]:
            return {"exit_price": last_price, "reason": "bollinger_lower_break"}

    else:  # short/sell
        if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
            return {"exit_price": last_price, "reason": "trend_reversal_ema_cross"}

        if not pd.isna(last["rsi"]) and last["rsi"] < 20:
            return {"exit_price": last_price, "reason": "rsi_oversold_exhaustion"}

        if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
            if not pd.isna(last["adx"]) and last["adx"] > 20:
                return {"exit_price": last_price, "reason": "macd_golden_cross"}

        if not pd.isna(last.get("supertrend_dir")) and not pd.isna(prev.get("supertrend_dir")):
            if prev["supertrend_dir"] == -1 and last["supertrend_dir"] == 1:
                return {"exit_price": last_price, "reason": "supertrend_bullish_flip"}

        if not pd.isna(last.get("kc_upper")) and last_price > last["kc_upper"]:
            if prev["close"] <= prev["kc_upper"]:
                return {"exit_price": last_price, "reason": "keltner_upper_break"}

        # Donchian upper break exit for shorts
        if not pd.isna(last.get("dc_upper")) and last_price > last["dc_upper"]:
            if prev["close"] <= prev["dc_upper"]:
                return {"exit_price": last_price, "reason": "donchian_upper_break"}

        profit_pct = (entry_price - peak_price) / entry_price * 100
        if profit_pct >= trailing_activate_pct:
            drawdown_from_peak = (last_price - peak_price) / peak_price * 100
            if drawdown_from_peak >= trailing_distance_pct:
                return {"exit_price": last_price, "reason": "trailing_stop_hit"}

        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width > 0.03 and last_price > last["bb_upper"]:
            return {"exit_price": last_price, "reason": "bollinger_upper_break"}

    return None


def generate_signal_with_ml(df: pd.DataFrame, risk_fraction_of_balance: float,
                             trading_balance: float, lstm_predictor=None,
                             ml_min_confidence: float = 0.6,
                             cfg: dict = None,
                             sentiment_score: float = None,
                             **kwargs) -> dict | None:
    """Full ensemble with optional ML filter and sentiment."""
    ml_prob = None
    if lstm_predictor is not None:
        ml_prob = lstm_predictor.predict_proba(df)

    base_signal = generate_signal(
        df, risk_fraction_of_balance, trading_balance,
        cfg=cfg, sentiment_score=sentiment_score,
        ml_probability=ml_prob,
    )

    if base_signal is None:
        return None

    if lstm_predictor is not None and ml_prob is not None:
        if base_signal["side"] == "buy":
            if ml_prob < ml_min_confidence:
                return None
            base_signal["ml_confidence"] = round(ml_prob, 3)
        else:
            if (1 - ml_prob) < ml_min_confidence:
                return None
            base_signal["ml_confidence"] = round(1 - ml_prob, 3)

    return base_signal


# --------------- Portfolio-Level Strategies ---------------

def detect_cross_exchange_arbitrage(prices: dict, min_spread_pct: float = 0.3) -> list[dict]:
    """
    Cross-exchange crypto arbitrage detection.
    prices: {exchange: {symbol: price}}
    Returns list of arbitrage opportunities.
    """
    opportunities = []
    exchanges = list(prices.keys())

    if len(exchanges) < 2:
        return opportunities

    # Collect all symbols across exchanges
    all_symbols = set()
    for ex_prices in prices.values():
        all_symbols.update(ex_prices.keys())

    for symbol in all_symbols:
        symbol_prices = {}
        for ex in exchanges:
            p = prices.get(ex, {}).get(symbol)
            if p is not None and p > 0:
                symbol_prices[ex] = p

        if len(symbol_prices) < 2:
            continue

        sorted_ex = sorted(symbol_prices.items(), key=lambda x: x[1])
        buy_ex, buy_price = sorted_ex[0]
        sell_ex, sell_price = sorted_ex[-1]

        spread_pct = (sell_price - buy_price) / buy_price * 100
        if spread_pct >= min_spread_pct:
            opportunities.append({
                "symbol": symbol,
                "buy_exchange": buy_ex,
                "sell_exchange": sell_ex,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "spread_pct": round(spread_pct, 4),
                "estimated_profit_pct": round(spread_pct - 0.2, 4),  # minus ~0.1% per side fees
                "strategies": ["cross_exchange_arbitrage"],
            })

    return opportunities


def detect_triangular_arbitrage(prices: dict, min_profit_pct: float = 0.1) -> list[dict]:
    """
    Triangular arbitrage detection across three currency pairs.
    prices: {pair: last_price} e.g. {"BTC/USDT": 60000, "ETH/BTC": 0.05, "ETH/USDT": 3000}
    """
    opportunities = []

    # Common triangular paths for crypto
    paths = [
        ("BTC/USDT", "ETH/BTC", "ETH/USDT"),  # USDT -> BTC -> ETH -> USDT
        ("BTC/USDT", "SOL/BTC", "SOL/USDT"),  # USDT -> BTC -> SOL -> USDT
        ("ETH/USDT", "SOL/ETH", "SOL/USDT"),  # USDT -> ETH -> SOL -> USDT
        ("ETH/USDT", "BTC/ETH", "BTC/USDT"),  # USDT -> ETH -> BTC -> USDT
    ]

    for p1, p2, p3 in paths:
        a = prices.get(p1)
        b = prices.get(p2)
        c = prices.get(p3)
        if a is None or b is None or c is None:
            continue
        if a <= 0 or b <= 0 or c <= 0:
            continue

        # Forward: buy p2 with p1, buy p3 with result
        # Start with 1 unit of p3's quote
        # 1 quote -> 1/a of p1 base -> (1/a) * (1/b) of p2 base -> (1/a) * (1/b) * c of quote
        forward = (1 / a) * (1 / b) * c
        forward_profit_pct = (forward - 1) * 100

        # Reverse
        reverse = a * b * (1 / c)
        reverse_profit_pct = (reverse - 1) * 100

        if forward_profit_pct > min_profit_pct:
            opportunities.append({
                "path": [p1, p2, p3],
                "direction": "forward",
                "profit_pct": round(forward_profit_pct, 4),
                "strategies": ["triangular_arbitrage"],
            })
        if reverse_profit_pct > min_profit_pct:
            opportunities.append({
                "path": [p1, p2, p3],
                "direction": "reverse",
                "profit_pct": round(reverse_profit_pct, 4),
                "strategies": ["triangular_arbitrage"],
            })

    return opportunities


def generate_portfolio_rotation_signal(
    symbol_returns: dict[str, pd.DataFrame],
    regime: str = "neutral",
    lookback_period: int = 20,
    rebalance_threshold: float = 0.6,
) -> dict | None:
    """
    Risk-parity / momentum rotation across multiple symbols.
    symbol_returns: {symbol: df} with OHLCV data
    Returns rotation signal: which symbols to overweight/underweight.
    """
    if not symbol_returns or len(symbol_returns) < 2:
        return None

    rankings = {}
    for symbol, df in symbol_returns.items():
        if df is None or len(df) < lookback_period:
            continue
        returns = df["close"].pct_change().dropna().tail(lookback_period)
        if len(returns) < 5:
            continue
        cum_return = (1 + returns).prod() - 1
        vol = returns.std() * np.sqrt(252)
        sharpe = cum_return / vol if vol > 0 else 0
        rankings[symbol] = {
            "cum_return": float(cum_return),
            "volatility": float(vol),
            "sharpe": float(sharpe),
        }

    if len(rankings) < 2:
        return None

    # Sort by Sharpe ratio
    sorted_syms = sorted(rankings.items(), key=lambda x: x[1]["sharpe"], reverse=True)

    # Top 30% = overweight (buy), bottom 30% = underweight (sell/avoid)
    n = len(sorted_syms)
    top_n = max(1, n // 3)
    bottom_n = max(1, n // 3)

    overweight = [s for s, _ in sorted_syms[:top_n]]
    underweight = [s for s, _ in sorted_syms[-bottom_n:]]

    return {
        "type": "portfolio_rotation",
        "overweight": overweight,
        "underweight": underweight,
        "rankings": {s: r for s, r in sorted_syms},
        "regime": regime,
        "strategies": ["risk_parity_rotation", "momentum_rotation"],
    }


def generate_dca_signal(
    df: pd.DataFrame,
    dca_interval_bars: int = 20,
    volatility_sizing: bool = True,
    cfg: dict = None,
) -> dict | None:
    """
    Dollar-cost averaging timing: regular buys with volatility-adjusted sizing.
    Increases buy size when volatility is low (better prices), decreases when high.
    """
    if df is None or len(df) < 50:
        return None

    cfg = cfg or {}
    df = compute_indicators(df, cfg)
    last = df.iloc[-1]

    atr_pct = last.get("vol_pct", 0)
    if pd.isna(atr_pct) or atr_pct <= 0:
        return None

    # Base DCA amount
    base_amount = cfg.get("dca_base_amount", 100)

    if volatility_sizing:
        # Historical median ATR%
        atr_hist = df["vol_pct"].dropna().tail(100)
        if len(atr_hist) < 20:
            median_vol = atr_hist.median()
        else:
            median_vol = atr_hist.median()

        vol_ratio = median_vol / atr_pct if atr_pct > 0 else 1.0
        # Scale between 0.5x and 2x base amount
        vol_scale = max(0.5, min(2.0, vol_ratio))
    else:
        vol_scale = 1.0

    adjusted_amount = base_amount * vol_scale

    # Buy signal — DCA always buys, but we check if it's a good time
    rsi = last.get("rsi", 50)
    buy_boost = 1.0
    if not pd.isna(rsi):
        if rsi < 30:
            buy_boost = 1.3  # extra buy when oversold
        elif rsi > 70:
            buy_boost = 0.7  # reduce buy when overbought

    final_amount = adjusted_amount * buy_boost

    return {
        "type": "dca",
        "side": "buy",
        "entry_price": float(last["close"]),
        "amount": round(final_amount, 2),
        "vol_scale": round(vol_scale, 2),
        "buy_boost": round(buy_boost, 2),
        "strategies": ["dca_timing"],
    }


def generate_safe_haven_rotation_signal(
    equity_df: pd.DataFrame,
    gold_df: pd.DataFrame,
    drawdown_threshold: float = -5.0,
) -> dict | None:
    """
    Safe-haven rotation: rotate into gold during equity/crypto drawdowns.
    equity_df: OHLCV of SPY, QQQ, or BTC
    gold_df: OHLCV of GLD or XAU/USD
    """
    if equity_df is None or gold_df is None:
        return None
    if len(equity_df) < 50 or len(gold_df) < 50:
        return None

    # Calculate recent drawdown
    equity_returns = equity_df["close"].pct_change().tail(20).dropna()
    cumulative_return = (1 + equity_returns).prod() - 1
    drawdown_pct = cumulative_return * 100

    gold_returns = gold_df["close"].pct_change().tail(20).dropna()
    gold_momentum = ((1 + gold_returns).prod() - 1) * 100

    # Equity in drawdown + gold showing positive momentum
    if drawdown_pct < drawdown_threshold and gold_momentum > 0:
        # Strong rotation signal
        strength = min(abs(drawdown_pct) / abs(drawdown_threshold), 2.0)
        return {
            "type": "safe_haven_rotation",
            "action": "rotate_to_gold",
            "equity_drawdown_pct": round(drawdown_pct, 2),
            "gold_momentum_pct": round(gold_momentum, 2),
            "strength": round(strength, 2),
            "strategies": ["safe_haven_rotation"],
        }

    # Gold losing momentum while equity recovers
    if drawdown_pct > 0 and gold_momentum < -2.0:
        return {
            "type": "safe_haven_rotation",
            "action": "rotate_to_equity",
            "equity_drawdown_pct": round(drawdown_pct, 2),
            "gold_momentum_pct": round(gold_momentum, 2),
            "strength": 0.5,
            "strategies": ["safe_haven_rotation"],
        }

    return None


def generate_options_signal(
    df: pd.DataFrame,
    implied_vol: float = None,
    cfg: dict = None,
) -> dict | None:
    """
    Options/derivatives signal detection:
    - Covered call opportunity: stock owned + IV high -> sell call
    - Straddle opportunity: IV very low before expected event -> buy straddle
    - Delta-neutral hedging signal
    """
    if df is None or len(df) < 50:
        return None

    cfg = cfg or {}
    df = compute_indicators(df, cfg)
    last = df.iloc[-1]

    signals = []

    # Covered call opportunity: high IV + stock in uptrend
    if implied_vol is not None and implied_vol > 0.30:
        rsi = last.get("rsi", 50)
        if not pd.isna(rsi) and 50 < rsi < 70:
            signals.append({
                "type": "covered_call",
                "action": "sell_call",
                "implied_vol": implied_vol,
                "reason": f"High IV ({implied_vol:.1%}) + neutral-bullish RSI ({rsi:.0f})",
                "strategies": ["options_covered_call"],
            })

    # Straddle opportunity: very low IV before potential event
    if implied_vol is not None and implied_vol < 0.15:
        bb_width = (last["bb_upper"] - last["bb_lower"]) / last["bb_mid"] if last["bb_mid"] > 0 else 0
        if bb_width < 0.03:
            signals.append({
                "type": "straddle",
                "action": "buy_straddle",
                "implied_vol": implied_vol,
                "reason": f"Low IV ({implied_vol:.1%}) + Bollinger squeeze (width={bb_width:.3f})",
                "strategies": ["options_straddle"],
            })

    # Volatility mean reversion: current vol very different from forecast
    vol_fc = last.get("vol_forecast")
    vol_pct = last.get("vol_pct")
    if not pd.isna(vol_fc) and not pd.isna(vol_pct) and vol_pct > 0:
        vol_ratio = vol_fc / vol_pct
        if vol_ratio > 2.0:
            signals.append({
                "type": "vol_spread",
                "action": "long_vol",
                "reason": f"Forecast vol {vol_fc:.1f}% much higher than current {vol_pct:.1f}%",
                "strategies": ["options_vol_spread"],
            })

    if signals:
        return {
            "type": "options_signals",
            "signals": signals,
            "strategies": [s["strategies"][0] for s in signals],
        }

    return None
