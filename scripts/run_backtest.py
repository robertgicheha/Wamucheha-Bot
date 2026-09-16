"""
Run a backtest against real historical OHLCV data before trusting the strategy
with money. Usage:

    python scripts/run_backtest.py --exchange binance --symbol BTC/USDT --timeframe 15m --limit 1000

Reports both full-period and walk-forward (train/test split) results. Pay closer
attention to the "test" numbers — that's the strategy performing on data it never
had a chance to be inadvertently tuned against.

Loads config/config.yaml so the backtest exercises the same strategy weights,
signal threshold, stop-loss/take-profit, and aggregator settings that main.py
would actually trade with — not a hardcoded stand-in that could silently drift
from what's configured to run live.
"""
import argparse
import sys
from pathlib import Path

import yaml

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
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--no-aggregator", action="store_true",
                         help="Disable the SignalAggregator conflict veto for comparison.")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)
    strategy_cfg = config.get("strategy", {})
    risk_cfg = config.get("risk", {})

    bt_kwargs = dict(
        initial_balance=args.balance,
        risk_fraction=risk_cfg.get("max_position_pct", 2) / 100,
        stop_loss_pct=risk_cfg.get("stop_loss_pct", 1.5),
        take_profit_pct=risk_cfg.get("take_profit_pct", 3.0),
        trailing_activate_pct=risk_cfg.get("trailing_stop_activate_pct", 1.5),
        trailing_distance_pct=risk_cfg.get("trailing_stop_distance_pct", 1.0),
        cfg=strategy_cfg,
        use_aggregator=not args.no_aggregator,
        min_aggregator_confidence=strategy_cfg.get("min_aggregator_confidence", 0.3),
    )

    md = MarketData(args.exchange)
    print(f"Fetching {args.limit} {args.timeframe} candles for {args.symbol} from {args.exchange}...")
    df = md.get_ohlcv(args.symbol, timeframe=args.timeframe, limit=args.limit)

    print(f"\nConfig: min_signal_score={strategy_cfg.get('min_signal_score', 0.18)} (0..1 scale), "
          f"SL={bt_kwargs['stop_loss_pct']}%, TP={bt_kwargs['take_profit_pct']}%, "
          f"aggregator={'on' if bt_kwargs['use_aggregator'] else 'off'}")

    print("\n=== Full period backtest ===")
    result = run_backtest(df, **bt_kwargs)
    for k, v in result.items():
        print(f"  {k}: {v}")

    print("\n=== Walk-forward (train/test split) ===")
    wf = walk_forward_backtest(df, **bt_kwargs)
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
