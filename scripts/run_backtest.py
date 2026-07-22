"""
Run a backtest against real historical OHLCV data before trusting the strategy
with money. Usage:

    python scripts/run_backtest.py --exchange binance --symbol BTC/USDT --timeframe 15m --limit 1000

Reports both full-period and walk-forward (train/test split) results. Pay closer
attention to the "test" numbers — that's the strategy performing on data it never
had a chance to be inadvertently tuned against.
"""
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from data_feeds.market_data import MarketData
from strategy.backtester import run_backtest, walk_forward_backtest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--balance", type=float, default=1000.0)
    args = parser.parse_args()

    md = MarketData(args.exchange)
    print(f"Fetching {args.limit} {args.timeframe} candles for {args.symbol} from {args.exchange}...")
    df = md.get_ohlcv(args.symbol, timeframe=args.timeframe, limit=args.limit)

    print("\n=== Full period backtest ===")
    result = run_backtest(df, initial_balance=args.balance)
    for k, v in result.items():
        print(f"  {k}: {v}")

    print("\n=== Walk-forward (train/test split) ===")
    wf = walk_forward_backtest(df, initial_balance=args.balance)
    print("Train (in-sample):")
    for k, v in wf["train"].items():
        print(f"  {k}: {v}")
    print("Test (out-of-sample — trust this more):")
    for k, v in wf["test"].items():
        print(f"  {k}: {v}")

    if wf["test"]["total_return_pct"] < wf["train"]["total_return_pct"] / 2:
        print("\n⚠️  Test-period return is much weaker than train-period. This is a "
              "classic overfitting signal — be skeptical of this strategy/timeframe "
              "combination before risking capital on it.")


if __name__ == "__main__":
    main()
