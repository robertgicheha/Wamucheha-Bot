"""
Train the LSTM price-direction predictor on real historical data and save it.

Usage:
    python ml/train_lstm.py --exchange binance --symbol BTC/USDT --timeframe 15m --limit 3000

Always check the printed test accuracy against the naive baseline before using
this model live — see the warning logic in LSTMPricePredictor.train().
"""
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from data_feeds.market_data import MarketData
from ml.lstm_predictor import LSTMPricePredictor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--epochs", type=int, default=20)
    args = parser.parse_args()

    md = MarketData(args.exchange)
    print(f"Fetching {args.limit} {args.timeframe} candles for {args.symbol}...")
    df = md.get_ohlcv(args.symbol, timeframe=args.timeframe, limit=args.limit)

    predictor = LSTMPricePredictor(symbol=args.symbol)
    print(f"Training LSTM on {args.symbol} ({args.timeframe})...")
    result = predictor.train(df, epochs=args.epochs)

    predictor.save()
    print(f"\nModel saved to ml/saved_models/lstm_{args.symbol.replace('/', '_')}.pt")
    print(f"Result summary: {result}")


if __name__ == "__main__":
    main()
