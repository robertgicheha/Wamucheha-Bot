"""
Backtest the crypto carry and trend modules on real exchange history.

    python scripts/run_crypto_backtest.py carry --exchange binance --symbol BTC/USDT:USDT --days 730
    python scripts/run_crypto_backtest.py trend --exchange binance --symbol BTC/USDT --days 1500

Both print an in-sample ("train", first 70%) and out-of-sample ("test", last
30%) result. Trust the test line, and only after you have NOT tuned parameters
against it. Read the module docstrings for what each backtest leaves out.

Survivorship bias: run this on coins that later died too before trusting any
cross-coin conclusion. BTC/ETH alone flatter the result.
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

sys.path.append(str(Path(__file__).parent.parent))

from strategy.funding_carry import CarryConfig, backtest_carry
from strategy.crypto_trend import TrendConfig, backtest_trend


def fetch_funding(exchange, symbol: str, days: int) -> pd.Series:
    since = exchange.milliseconds() - days * 86_400_000
    rows = []
    while since < exchange.milliseconds():
        batch = exchange.fetch_funding_rate_history(symbol, since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1]["timestamp"] + 1
        if nxt <= since:
            break
        since = nxt
        time.sleep(exchange.rateLimit / 1000)
    s = pd.Series({pd.to_datetime(r["timestamp"], unit="ms"): r["fundingRate"] for r in rows})
    return s[~s.index.duplicated()].sort_index()


def fetch_daily(exchange, symbol: str, days: int) -> pd.DataFrame:
    since = exchange.milliseconds() - days * 86_400_000
    rows = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, "1d", since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 2 or batch[-1][0] <= since:
            break
        since = batch[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000)
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.drop_duplicates("timestamp").set_index("timestamp").sort_index()


def _show(label: str, res: dict, keys: list):
    print(f"  {label:<6}" + "  ".join(f"{k}={res[k]}" for k in keys))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("module", choices=["carry", "trend"])
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", default=None)
    p.add_argument("--days", type=int, default=1000)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()

    import ccxt
    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True})

    cfg_all = {}
    try:
        with open(args.config) as f:
            cfg_all = (yaml.safe_load(f) or {}).get("crypto_modules", {})
    except FileNotFoundError:
        pass

    if args.module == "carry":
        symbol = args.symbol or "BTC/USDT:USDT"
        carry_cfg = CarryConfig(**cfg_all.get("carry", {}))
        funding = fetch_funding(ex, symbol, args.days)
        print(f"{symbol} on {args.exchange}: {len(funding)} funding prints")
        cut = int(len(funding) * 0.7)
        keys = ["annualized_return_pct", "max_drawdown_pct", "trades",
                "funding_collected", "costs_paid", "time_in_market_pct"]
        _show("full", backtest_carry(funding, carry_cfg, args.balance), keys)
        _show("train", backtest_carry(funding.iloc[:cut], carry_cfg, args.balance), keys)
        _show("test", backtest_carry(funding.iloc[cut:], carry_cfg, args.balance), keys)
    else:
        symbol = args.symbol or "BTC/USDT"
        trend_cfg = TrendConfig(**cfg_all.get("trend", {}))
        df = fetch_daily(ex, symbol, args.days)
        print(f"{symbol} on {args.exchange}: {len(df)} daily bars")
        cut = int(len(df) * 0.7)
        keys = ["annualized_return_pct", "max_drawdown_pct", "sharpe", "trades",
                "win_rate_pct", "costs_paid", "buy_hold_return_pct"]
        _show("full", backtest_trend(df, trend_cfg, args.balance), keys)
        _show("train", backtest_trend(df.iloc[:cut], trend_cfg, args.balance), keys)
        _show("test", backtest_trend(df.iloc[cut:], trend_cfg, args.balance), keys)


if __name__ == "__main__":
    main()
