"""Where this module keeps its files (NSE price cache and recorded history)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    # Docker mounts data/ read-only into the dashboard, so compose points this elsewhere.
    return Path(os.environ.get("STOCKS_DATA_DIR") or ROOT / "data" / "stocks")
