"""
TensorFlow/Keras LSTM price-direction predictor.

Alternative backend to PyTorch LSTM. Both use the same enriched 15-feature set
and label definition for apples-to-apples comparison. Selected via
config.yaml -> ml.lstm_model_type: tensorflow.

Same honest scope:
- Predicts PROBABILITY of price being higher N bars ahead
- Does NOT predict price targets
- Wired in as a filter alongside rule-based strategy
- Risk manager still has final veto power
"""
import numpy as np
import pandas as pd
from pathlib import Path

try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers
    _has_tf = True
except ImportError:
    _has_tf = False

MODEL_DIR = Path(__file__).parent / "saved_models"
MODEL_DIR.mkdir(exist_ok=True)

# Same 15-feature set as the PyTorch version
FEATURE_COLS = [
    "return", "high_low_range", "volume_change",
    "rsi_norm", "macd_hist_norm", "bb_pct_b",
    "adx_norm", "stoch_k_norm", "obv_momentum",
    "atr_pct", "ema_alignment", "price_vs_ema50",
    "price_vs_ema200", "volume_ratio_norm", "momentum_5",
]
LOOKBACK = 30
HORIZON = 5


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Same enriched feature engineering as the PyTorch version."""
    df = df.copy()

    df["return"] = df["close"].pct_change()
    df["high_low_range"] = (df["high"] - df["low"]) / df["close"]
    df["volume_change"] = df["volume"].pct_change()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    df["rsi_norm"] = (rsi - 50) / 50

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line
    df["macd_hist_norm"] = macd_hist / df["close"]

    bb_mid = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    df["bb_pct_b"] = (df["close"] - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)

    plus_dm = df["high"].diff()
    minus_dm = -df["low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.rolling(14).mean()
    df["adx_norm"] = adx / 100

    low_min = df["low"].rolling(14).min()
    high_max = df["high"].rolling(14).max()
    df["stoch_k_norm"] = (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)

    obv = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    obv_ema = obv.ewm(span=20, adjust=False).mean()
    df["obv_momentum"] = (obv - obv_ema) / obv_ema.abs().replace(0, np.nan)

    df["atr_pct"] = atr / df["close"]

    ema9 = df["close"].ewm(span=9, adjust=False).mean()
    ema21 = df["close"].ewm(span=21, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    ema200 = df["close"].ewm(span=200, adjust=False).mean()
    df["ema_alignment"] = (
        (df["close"] > ema9).astype(float) +
        (df["close"] > ema21).astype(float) +
        (df["close"] > ema50).astype(float) +
        (df["close"] > ema200).astype(float)
    ) / 2 - 1

    df["price_vs_ema50"] = (df["close"] - ema50) / ema50
    df["price_vs_ema200"] = (df["close"] - ema200) / ema200

    vol_avg20 = df["volume"].rolling(20).mean()
    df["volume_ratio_norm"] = (df["volume"] / vol_avg20.replace(0, np.nan)).clip(0, 5) / 5

    df["momentum_5"] = df["close"].pct_change(5)

    return df.dropna()


def _make_sequences(features: np.ndarray, closes: np.ndarray,
                     lookback: int, horizon: int):
    X, y = [], []
    for i in range(lookback, len(features) - horizon):
        X.append(features[i - lookback:i])
        future_return = (closes[i + horizon] - closes[i]) / closes[i]
        y.append(1.0 if future_return > 0 else 0.0)
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32)


class TFLSTMPricePredictor:
    def __init__(self, symbol: str = "default", lookback: int = LOOKBACK,
                 horizon: int = HORIZON):
        if not _has_tf:
            raise ImportError("TensorFlow not installed: pip install tensorflow")
        self.symbol = symbol
        self.lookback = lookback
        self.horizon = horizon
        self.model = self._build_model(len(FEATURE_COLS))
        self._feature_mean = None
        self._feature_std = None
        self._is_trained = False

    def _model_path(self) -> Path:
        return MODEL_DIR / f"tf_lstm_{self.symbol.replace('/', '_')}.keras"

    def _meta_path(self) -> Path:
        return MODEL_DIR / f"tf_lstm_{self.symbol.replace('/', '_')}.npz"

    def _build_model(self, n_features: int) -> keras.Model:
        model = keras.Sequential([
            layers.Input(shape=(self.lookback, n_features)),
            layers.LSTM(48, return_sequences=True),
            layers.Dropout(0.3),
            layers.LSTM(32),
            layers.Dropout(0.3),
            layers.Dense(1, activation="sigmoid"),
        ])
        model.compile(
            optimizer=keras.optimizers.Adam(learning_rate=5e-4, weight_decay=1e-5),
            loss="binary_crossentropy",
            metrics=["accuracy"],
        )
        return model

    def train(self, df: pd.DataFrame, epochs: int = 30, lr: float = 5e-4, train_frac: float = 0.7,
              batch_size: int = 32, verbose: bool = 1) -> dict:
        feat_df = _build_features(df)
        # Dynamically set learning rate if caller overrides the default
        if lr != 5e-4:
            keras.backend.set_value(self.model.optimizer.learning_rate, lr)
        if len(feat_df) < self.lookback + self.horizon + 50:
            raise ValueError("Not enough data — need ~100+ bars after feature warmup.")

        features = feat_df[FEATURE_COLS].values
        self._feature_mean = features.mean(axis=0)
        self._feature_std = features.std(axis=0) + 1e-8
        features_norm = (features - self._feature_mean) / self._feature_std

        X, y = _make_sequences(features_norm, feat_df["close"].values,
                               self.lookback, self.horizon)
        split = int(len(X) * train_frac)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        callbacks = [
            keras.callbacks.EarlyStopping(
                monitor="val_loss", patience=6, restore_best_weights=True
            ),
        ]

        self.model.fit(
            X_train, y_train,
            validation_data=(X_test, y_test),
            epochs=epochs,
            batch_size=batch_size,
            callbacks=callbacks,
            verbose=verbose,
        )

        test_loss, test_acc = self.model.evaluate(X_test, y_test, verbose=0)
        baseline_acc = max(y_test.mean(), 1 - y_test.mean())

        self._is_trained = True
        result = {
            "test_accuracy": round(float(test_acc), 4),
            "test_loss": round(float(test_loss), 4),
            "naive_baseline_accuracy": round(float(baseline_acc), 4),
            "n_train_samples": len(X_train),
            "n_test_samples": len(X_test),
        }

        if verbose:
            print(f"\nTest accuracy: {result['test_accuracy']}")
            print(f"Naive baseline: {result['naive_baseline_accuracy']}")
            if result["test_accuracy"] <= result["naive_baseline_accuracy"] + 0.02:
                print("WARNING: Model barely beats naive baseline.")
        return result

    def predict_proba(self, df: pd.DataFrame) -> float | None:
        if not self._is_trained:
            return None
        feat_df = _build_features(df)
        if len(feat_df) < self.lookback:
            return None

        features = feat_df[FEATURE_COLS].values[-self.lookback:]
        features_norm = (features - self._feature_mean) / self._feature_std
        X = np.expand_dims(features_norm.astype(np.float32), axis=0)

        prob = self.model(X, training=False).numpy()[0][0]
        return float(prob)

    def save(self):
        self.model.save(self._model_path())
        np.savez(
            self._meta_path(),
            feature_mean=self._feature_mean,
            feature_std=self._feature_std,
            lookback=self.lookback,
            horizon=self.horizon,
        )

    def load(self) -> bool:
        model_path = self._model_path()
        meta_path = self._meta_path()
        if not model_path.exists() or not meta_path.exists():
            return False
        self.model = keras.models.load_model(model_path)
        meta = np.load(meta_path)
        self._feature_mean = meta["feature_mean"]
        self._feature_std = meta["feature_std"]
        self.lookback = int(meta["lookback"])
        self.horizon = int(meta["horizon"])
        self._is_trained = True
        return True