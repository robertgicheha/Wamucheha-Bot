"""
LSTM-based short-horizon price direction predictor (PyTorch).

Predicts a PROBABILITY of price being higher N bars ahead, from a window of
recent OHLCV + enriched technical features. Used as a secondary filter
alongside the rule-based strategy ensemble — it can only veto trades, never
create them.

Enhanced feature set (15 features vs original 4):
- Price returns, high-low range, volume change
- RSI, MACD histogram, Bollinger %B
- ADX trend strength, Stochastic %K
- OBV momentum, ATR volatility
- EMA alignment, price vs EMA50/200
"""
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from pathlib import Path

MODEL_DIR = Path(__file__).parent / "saved_models"
MODEL_DIR.mkdir(exist_ok=True)

FEATURE_COLS = [
    "return", "high_low_range", "volume_change",
    "rsi_norm", "macd_hist_norm", "bb_pct_b",
    "adx_norm", "stoch_k_norm", "obv_momentum",
    "atr_pct", "ema_alignment", "price_vs_ema50",
    "price_vs_ema200", "volume_ratio_norm", "momentum_5",
]
LOOKBACK = 30
HORIZON = 5


class LSTMNet(nn.Module):
    def __init__(self, n_features: int, hidden_size: int = 48, num_layers: int = 2):
        super().__init__()
        self.lstm = nn.LSTM(n_features, hidden_size, num_layers, batch_first=True, dropout=0.3)
        self.attention = nn.Linear(hidden_size, 1)
        self.fc = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        out, _ = self.lstm(x)
        attn_weights = torch.softmax(self.attention(out), dim=1)
        context = (out * attn_weights).sum(dim=1)
        return self.sigmoid(self.fc(context))


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

    def train(self, df: pd.DataFrame, epochs: int = 30, lr: float = 5e-4,
              train_frac: float = 0.7, verbose: bool = True) -> dict:
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

        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr, weight_decay=1e-5)
        loss_fn = nn.BCELoss()

        best_acc = 0
        patience_counter = 0
        self.model.train()
        for epoch in range(epochs):
            optimizer.zero_grad()
            preds = self.model(X_train_t)
            loss = loss_fn(preds, y_train_t)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            optimizer.step()

            if (epoch + 1) % 5 == 0:
                self.model.eval()
                with torch.no_grad():
                    test_preds = self.model(X_test_t)
                    test_acc = ((test_preds > 0.5).float() == y_test_t).float().mean().item()
                if test_acc > best_acc:
                    best_acc = test_acc
                    patience_counter = 0
                else:
                    patience_counter += 1
                if patience_counter >= 6:
                    if verbose:
                        print(f"  Early stopping at epoch {epoch+1}")
                    break
                self.model.train()
                if verbose:
                    print(f"  epoch {epoch+1}/{epochs} — loss: {loss.item():.4f} — test_acc: {test_acc:.4f}")

        self.model.eval()
        with torch.no_grad():
            test_preds = self.model(X_test_t)
            test_acc = ((test_preds > 0.5).float() == y_test_t).float().mean().item()
            baseline_acc = max(y_test.mean(), 1 - y_test.mean())

        self._is_trained = True
        result = {
            "test_accuracy": round(test_acc, 4),
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
        try:
            checkpoint = torch.load(path, weights_only=True)
        except Exception:
            checkpoint = torch.load(path, weights_only=False)
        self.model.load_state_dict(checkpoint["model_state"])
        self._feature_mean = checkpoint["feature_mean"]
        self._feature_std = checkpoint["feature_std"]
        self.lookback = checkpoint["lookback"]
        self.horizon = checkpoint["horizon"]
        self._is_trained = True
        return True
