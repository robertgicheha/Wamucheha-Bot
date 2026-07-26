"""
LSTM price-direction predictor — dispatcher module.

Routes to either PyTorch or TensorFlow backend based on config.yaml:
    ml.lstm_model_type: pytorch   (default)
    ml.lstm_model_type: tensorflow

Both backends share the same 15-feature set and label definition.
The dispatcher transparently loads whichever model type was saved for each symbol.
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


def _get_model_type() -> str:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("ml", {}).get("lstm_model_type", "pytorch")
    except Exception:
        return "pytorch"


class LSTMPricePredictor:
    """
    Public interface identical to the standalone PyTorch predictor.
    Delegates to either PyTorch or TensorFlow implementation at init time.
    """

    def __init__(self, symbol: str = "default", lookback: int = 30, horizon: int = 5):
        self.symbol = symbol
        model_type = _get_model_type()

        if model_type == "tensorflow":
            from ml.tf_lstm_predictor import TFLSTMPricePredictor
            self._backend = TFLSTMPricePredictor(symbol=symbol, lookback=lookback, horizon=horizon)
            self._backend_type = "tensorflow"
        else:
            from ml.lstm_torch import LSTMPricePredictor as _PtPredictor
            self._backend = _PtPredictor(symbol=symbol, lookback=lookback, horizon=horizon)
            self._backend_type = "pytorch"

    @property
    def model_type(self) -> str:
        return self._backend_type

    def train(self, df, epochs=30, lr=5e-4, train_frac=0.7, verbose=True, **kwargs):
        return self._backend.train(df, epochs=epochs, train_frac=train_frac, verbose=verbose)

    def predict_proba(self, df):
        return self._backend.predict_proba(df)

    def save(self):
        self._backend.save()

    def load(self) -> bool:
        return self._backend.load()
