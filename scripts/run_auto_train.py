"""
Retrain the daily trend bot's settings (see core/auto_train.py for the rules).

  python -m scripts.run_auto_train               # retrain now
  python -m scripts.run_auto_train --dry-report  # run every test, change nothing
  python -m scripts.run_auto_train --scheduled   # only if due (the bot calls this itself)
  python -m scripts.run_auto_train --status
  python -m scripts.run_auto_train --rollback

History comes from the MT5 terminal; if it is unavailable or too short, from Yahoo Finance
(for gold that is the COMEX futures price, close to but not the same as your broker's spot).
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import auto_train  # noqa: E402
from strategy.fx_trend import TrendParams  # noqa: E402

YAHOO = {"XAUUSD": "GC=F", "XAGUSD": "SI=F", "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"}


def load_config(path: Path) -> tuple:
    y = yaml.safe_load(path.read_text(encoding="utf-8"))["fx_trend"]
    return y, TrendParams(**y["strategy"]), auto_train.settings(y.get("auto_train"))


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Closed daily bars with sane prices: today's bar is still forming, so it is dropped."""
    df = df[["open", "high", "low", "close"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df > 0).all(axis=1) & (df["high"] >= df["low"])]
    return df[df.index < pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()]


def from_mt5(y: dict, bars: int) -> dict:
    """Read-only use of the terminal: no orders are possible from here."""
    from core.fx_trend_broker import MT5Broker
    broker = MT5Broker(magic=y["magic"], suffix=y.get("symbol_suffix", ""),
                       login=int(os.environ.get("MT5_LOGIN", "0") or 0),
                       password=os.environ.get("MT5_PASSWORD", ""), server=os.environ.get("MT5_SERVER", ""))
    if not broker.connect():
        raise RuntimeError("MT5 connection failed")
    return {pair: clean(broker.daily_bars(pair, bars)) for pair in y["pairs"]}


def from_yahoo(pairs: list) -> dict:
    import yfinance as yf
    frames = {}
    for pair in pairs:
        symbol = YAHOO.get(pair, pair + "=X")
        df = yf.Ticker(symbol).history(period="max", interval="1d", timeout=30)
        if df is None or df.empty:
            raise RuntimeError(f"Yahoo Finance returned no history for {symbol}")
        frames[pair] = clean(df.rename(columns=str.lower))
    return frames


def load_history(y: dict, cfg: dict, source: str) -> tuple:
    years = cfg["train_years"] + cfg["holdout_years"] + 3      # a few years of unseen test folds
    if source in ("auto", "mt5"):
        try:
            frames = from_mt5(y, cfg["history_bars"])
            short = [p for p, df in frames.items() if len(df) < years * 250]
            if not short:
                return frames, "MT5 terminal"
            raise RuntimeError(f"MT5 has too little history for {', '.join(short)}")
        except Exception as e:
            if source == "mt5":
                raise
            print(f"MT5 history not used: {e}")
    return from_yahoo(y["pairs"]), "Yahoo Finance"


def show(report: dict) -> None:
    print(auto_train.summary(report))
    print(f"\nData {report['data_from']} to {report['data_to']} from {report['data_source']}; "
          f"hold-out from {report['holdout_from']}; {report['candidates']} candidates.")
    for key in ("champion", "challenger"):
        block = report[key]
        h, f = block["holdout"], block["full_period"]
        print(f"\n{key.upper()}  {block['params']}")
        for name, s in (("hold-out", h), ("full period", f)):
            print(f"  {name:12s} return {s.get('total_return_pct')}%  worst drawdown {s.get('max_drawdown_pct')}%  "
                  f"Sharpe {s.get('sharpe')}  trades {s.get('n_trades')}  profit factor {s.get('profit_factor')}")
    w = report["walk_forward"]
    print(f"\nWALK-FORWARD (unseen years only): return {w.get('total_return_pct')}%  per year {w.get('cagr_pct')}%  "
          f"worst drawdown {w.get('max_drawdown_pct')}%  Sharpe {w.get('sharpe')}  trades {w.get('n_trades')}")
    print("\nGates:")
    for c in report["checks"]:
        print(f"  {'PASS' if c['passed'] else 'FAIL'}  {c['gate']}: {c['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrain the daily trend bot's settings")
    parser.add_argument("--config", default=str(ROOT / "config" / "fx_trend.yaml"))
    parser.add_argument("--source", choices=["auto", "mt5", "yahoo"], default="auto")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--scheduled", action="store_true")
    action.add_argument("--dry-report", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    y, base, cfg = load_config(Path(args.config))
    store = auto_train.Store(ROOT)
    if args.rollback:
        print(auto_train.rollback(store))
        return 0
    if args.status:
        print(json.dumps({"status": store.read(store.status_file), "promoted": store.read(store.champion_file)},
                         indent=2))
        return 0
    if args.scheduled and not auto_train.is_due(store, cfg):
        print("not due yet")
        return 0

    if not args.dry_report:
        auto_train.mark_attempt(store)
    try:
        frames, source = load_history(y, cfg, args.source)
        if args.dry_report:
            champion = auto_train.champion_params(base, store.read(store.champion_file)) or base
            report = auto_train.evaluate(frames, champion, cfg, base=base)
            report.update(data_source=source, decision="dry report, nothing changed")
        else:
            report = auto_train.train(frames, base, store, cfg, source)
    except Exception as e:
        print(f"Auto-training failed: {type(e).__name__}: {e}")
        if not args.dry_report:
            store.log("failed", error=str(e)[:300])
        return 1
    show(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
