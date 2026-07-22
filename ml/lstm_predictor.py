"""
LSTM-based short-horizon price direction predictor (PyTorch).

Honest scope of what this does and doesn't do:
- It predicts a PROBABILITY of price being higher N bars ahead, from a window of
  recent OHLCV + technical features. It does NOT predict a price target, and it
  should never be trusted as a standalone trade trigger.
- Public price/volume history is heavily arbitraged. Treat any backtested accuracy
  above ~55-58% on unseen data with real suspicion — it usually means leakage
  (e.g. features computed using future data) rather than a genuine edge.
- This is wired in as ONE additional filter alongside the existing rule-based
  strategy (strategy/technical_strategy.py), not a replacement for it. The risk
  manager still has final veto power on every trade regardless of what this
  predicts — see core/risk_manager.py, unchanged by this module.

Usage:
    predictor = LSTMPricePredictor()
    predictor.train(df)                      # df = OHLCV DataFrame
    prob_up = predictor.predict_proba(df)     # float 0-1, most recent window
"""
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from pathlib import Path

MODEL_DIR = Path(__file__).parent / "saved_models"
MODEL_DIR.mkdir(exist_ok=True)

FEATURE_COLS = ["return", "high_low_range", "volume_change", "rsi_norm"]
LOOKBACK = 30            # bars of history fed into the LSTM per prediction
HORIZON = 5              # bars ahead the label looks (price up or down by then)


class LSTMNet(nn.Module):
    def __init__(self, n_features: int, hidden_size: int = 32, num_layers: int = 2):
        super().__init__()
        self.lstm = nn.LSTM(n_features, hidden_size, num_layers, batch_first=True, dropout=0.2)
        self.fc = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        out, _ = self.lstm(x)
        last_step = out[:, -1, :]
        return self.sigmoid(self.fc(last_step))


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["return"] = df["close"].pct_change()
    df["high_low_range"] = (df["high"] - df["low"]) / df["close"]
    df["volume_change"] = df["volume"].pct_change()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    df["rsi_norm"] = (rsi - 50) / 50  # normalize to roughly [-1, 1]

    return df.dropna()


def _make_sequences(features: np.ndarray, closes: np.ndarray,
                     lookback: int, horizon: int):
    X, y = [], []
    for i in range(lookback, len(features) - horizon):
        X.append(features[i - lookback:i])
        future_return = (closes[i + horizon] - closes[i]) / closes[i]
        y.append(1.0 if future_return > 0 else 0.0)
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32)


class LSTMPricePredictor:
    def __init__(self, symbol: str = "default", lookback: int = LOOKBACK, horizon: int = HORIZON):
        self.symbol = symbol
        self.lookback = lookback
        self.horizon = horizon
        self.model = LSTMNet(n_features=len(FEATURE_COLS))
        self._feature_mean = None
        self._feature_std = None
        self._is_trained = False

    def _model_path(self) -> Path:
        return MODEL_DIR / f"lstm_{self.symbol.replace('/', '_')}.pt"

    def train(self, df: pd.DataFrame, epochs: int = 20, lr: float = 1e-3,
              train_frac: float = 0.7, verbose: bool = True) -> dict:
        """
        Trains on a chronological train split only, and reports accuracy on the
        held-out chronological test split — never shuffle time-series data, or
        the accuracy number is meaningless (the model would be peeking at the future).
        """
        feat_df = _build_features(df)
        if len(feat_df) < self.lookback + self.horizon + 50:
            raise ValueError("Not enough data to train — need at least ~100 bars after feature warmup.")

        features = feat_df[FEATURE_COLS].values
        self._feature_mean = features.mean(axis=0)
        self._feature_std = features.std(axis=0) + 1e-8
        features_norm = (features - self._feature_mean) / self._feature_std

        X, y = _make_sequences(features_norm, feat_df["close"].values, self.lookback, self.horizon)
        split = int(len(X) * train_frac)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        X_train_t = torch.from_numpy(X_train)
        y_train_t = torch.from_numpy(y_train).unsqueeze(1)
        X_test_t = torch.from_numpy(X_test)
        y_test_t = torch.from_numpy(y_test).unsqueeze(1)

        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        loss_fn = nn.BCELoss()

        self.model.train()
        for epoch in range(epochs):
            optimizer.zero_grad()
            preds = self.model(X_train_t)
            loss = loss_fn(preds, y_train_t)
            loss.backward()
            optimizer.step()
            if verbose and (epoch + 1) % 5 == 0:
                print(f"  epoch {epoch+1}/{epochs} — train loss: {loss.item():.4f}")

        self.model.eval()
        with torch.no_grad():
            test_preds = self.model(X_test_t)
            test_acc = ((test_preds > 0.5).float() == y_test_t).float().mean().item()
            baseline_acc = max(y_test.mean(), 1 - y_test.mean())  # always-predict-majority-class

        self._is_trained = True
        result = {
            "test_accuracy": round(test_acc, 4),
            "naive_baseline_accuracy": round(float(baseline_acc), 4),
            "n_train_samples": len(X_train),
            "n_test_samples": len(X_test),
        }
        if verbose:
            print(f"\nTest accuracy: {result['test_accuracy']}")
            print(f"Naive baseline (always predict majority class): {result['naive_baseline_accuracy']}")
            if result["test_accuracy"] <= result["naive_baseline_accuracy"] + 0.02:
                print("⚠️  Model is barely beating (or losing to) the naive baseline — "
                      "this suggests no real predictive edge on this data. Do not "
                      "use this model's output as a trade trigger.")
        return result

    def predict_proba(self, df: pd.DataFrame) -> float | None:
        """Returns probability of price being higher `horizon` bars ahead, using
        the most recent `lookback` bars. Returns None if not enough data or the
        model hasn't been trained/loaded."""
        if not self._is_trained:
            return None
        feat_df = _build_features(df)
        if len(feat_df) < self.lookback:
            return None

        features = feat_df[FEATURE_COLS].values[-self.lookback:]
        features_norm = (features - self._feature_mean) / self._feature_std
        X = torch.from_numpy(features_norm.astype(np.float32)).unsqueeze(0)

        self.model.eval()
        with torch.no_grad():
            prob = self.model(X).item()
        return prob

    def save(self):
        torch.save({
            "model_state": self.model.state_dict(),
            "feature_mean": self._feature_mean,
            "feature_std": self._feature_std,
            "lookback": self.lookback,
            "horizon": self.horizon,
        }, self._model_path())

    def load(self) -> bool:
        path = self._model_path()
        if not path.exists():
            return False
        checkpoint = torch.load(path, weights_only=False)
        self.model.load_state_dict(checkpoint["model_state"])
        self._feature_mean = checkpoint["feature_mean"]
        self._feature_std = checkpoint["feature_std"]
        self.lookback = checkpoint["lookback"]
        self.horizon = checkpoint["horizon"]
        self._is_trained = True
        return True
