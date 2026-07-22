"""
TensorFlow/Keras LSTM price-direction predictor.

This is an alternative to the PyTorch LSTM in ml/lstm_predictor.py. Both use
the same feature set and label definition so you can compare architectures
apples-to-apples on the same data.

Same honest scope as the PyTorch version:
- Predicts PROBABILITY of price being higher N bars ahead
- Does NOT predict price targets
- Should NOT be used as a standalone trade trigger
- Wired in as an additional filter alongside rule-based strategy
- Risk manager still has final veto power regardless of this model's output

Usage:
    predictor = TFLSTMPricePredictor(symbol="BTC/USDT")
    predictor.train(df)
    prob_up = predictor.predict_proba(df)
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

FEATURE_COLS = ["return", "high_low_range", "volume_change", "rsi_norm"]
LOOKBACK = 30
HORIZON = 5


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Same feature engineering as the PyTorch version for consistency."""
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
            raise ImportError(
                "TensorFlow is not installed. Install with: pip install tensorflow"
            )
        self.symbol = symbol
        self.lookback = lookback
        self.horizon = horizon
        self.model = self._build_model(len(FEATURE_COLS))
        self._feature_mean = None
        self._feature_std = None
        self._is_trained = False

    def _model_path(self) -> Path:
        return MODEL_DIR / f"tf_lstm_{self.symbol.replace('/', '_')}.keras"

    def _build_model(self, n_features: int) -> keras.Model:
        model = keras.Sequential([
            layers.Input(shape=(self.lookback, n_features)),
            layers.LSTM(32, return_sequences=True),
            layers.Dropout(0.2),
            layers.LSTM(16),
            layers.Dropout(0.2),
            layers.Dense(1, activation="sigmoid"),
        ])
        model.compile(
            optimizer=keras.optimizers.Adam(learning_rate=1e-3),
            loss="binary_crossentropy",
            metrics=["accuracy"],
        )
        return model

    def train(self, df: pd.DataFrame, epochs: int = 20, train_frac: float = 0.7,
              batch_size: int = 32, verbose: bool = 1) -> dict:
        """
        Trains on chronological train split, evaluates on held-out test split.
        Never shuffles time-series data.
        """
        feat_df = _build_features(df)
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
                monitor="val_loss", patience=5, restore_best_weights=True
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
                print("WARNING: Model barely beats naive baseline — no real predictive edge detected.")

        return result

    def predict_proba(self, df: pd.DataFrame) -> float | None:
        """Returns probability of price being higher `horizon` bars ahead."""
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
            self._model_path().with_suffix(".npz"),
            feature_mean=self._feature_mean,
            feature_std=self._feature_std,
            lookback=self.lookback,
            horizon=self.horizon,
        )

    def load(self) -> bool:
        model_path = self._model_path()
        meta_path = model_path.with_suffix(".npz")
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
