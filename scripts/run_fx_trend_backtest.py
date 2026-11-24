"""
Backtest the daily FX trend-following strategy (strategy/fx_trend.py).

Data sources (pick one):
  --csv-dir DIR   one CSV per pair named e.g. EURUSD.csv with columns
                  time,open,high,low,close  (Dukascopy / broker export)
  --mt5           pull daily bars from the connected MT5 terminal

Honest-testing workflow this script supports:
  1. Costs are ON by default (spread, slippage, swap).
  2. --holdout-start splits history: the strategy is judged on the period
     AFTER that date, which you look at ONCE. Re-tuning after seeing it
     turns it back into in-sample data.
  3. A small parameter-sensitivity grid: if only one exact setting is
     profitable, the edge is fragile.
  4. Monte Carlo on the trade sequence for a realistic worst-case drawdown.

Usage:
  python scripts/run_fx_trend_backtest.py --csv-dir data/fx_daily --holdout-start 2022-01-01
  python scripts/run_fx_trend_backtest.py --mt5 --bars 5000
"""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.append(str(ROOT))

from strategy.fx_trend import TrendParams
from strategy.fx_trend_backtest import BtConfig, monte_carlo, run_backtest, stats


def _downsample(curve: pd.Series, n: int = 400) -> list[dict]:
    if curve.empty:
        return []
    step = max(len(curve) // n, 1)
    s = curve.iloc[::step]
    if s.index[-1] != curve.index[-1]:
        s = pd.concat([s, curve.iloc[[-1]]])
    return [{"t": str(i.date()), "v": round(float(v), 2)} for i, v in s.items()]

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY", "GBPJPY"]


def load_csv_dir(path: Path, pairs: list[str]) -> dict[str, pd.DataFrame]:
    frames = {}
    for pair in pairs:
        f = path / f"{pair}.csv"
        if not f.exists():
            print(f"  skip {pair}: {f} not found")
            continue
        df = pd.read_csv(f)
        df.columns = [c.lower() for c in df.columns]
        tcol = "time" if "time" in df.columns else df.columns[0]
        df[tcol] = pd.to_datetime(df[tcol], utc=True).dt.tz_localize(None).dt.normalize()
        frames[pair] = df.set_index(tcol).sort_index()[["open", "high", "low", "close"]].dropna()
    return frames


def load_mt5(pairs: list[str], bars: int) -> dict[str, pd.DataFrame]:
    from data_feeds.mt5_feed import MT5Feed
    feed = MT5Feed()
    if not feed.connect():
        sys.exit("Could not connect to MT5 terminal.")
    frames = {}
    for pair in pairs:
        df = feed.get_ohlcv(pair, "1d", bars)
        if df.empty:
            print(f"  skip {pair}: no data (check the broker's symbol name/suffix)")
            continue
        df.index = df.index.tz_localize(None).normalize()
        frames[pair] = df[["open", "high", "low", "close"]]
    return frames


def show(title: str, res: dict):
    print(f"\n=== {title} ===")
    for k, v in res["stats"].items():
        print(f"  {k}: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", type=Path)
    ap.add_argument("--mt5", action="store_true")
    ap.add_argument("--bars", type=int, default=4000)
    ap.add_argument("--pairs", nargs="+", default=DEFAULT_PAIRS)
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--risk-pct", type=float, default=0.5)
    ap.add_argument("--holdout-start", default=None, help="YYYY-MM-DD; judge on data from here on")
    ap.add_argument("--no-grid", action="store_true")
    args = ap.parse_args()

    if args.csv_dir:
        frames = load_csv_dir(args.csv_dir, args.pairs)
    elif args.mt5:
        frames = load_mt5(args.pairs, args.bars)
    else:
        ap.error("pass --csv-dir or --mt5")
    if not frames:
        sys.exit("No data loaded.")

    p = TrendParams(risk_pct=args.risk_pct)
    base = BtConfig(initial_equity=args.equity)
    report = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "source": f"csv:{args.csv_dir}" if args.csv_dir else "mt5",
              "pairs": sorted(frames), "params": asdict(p), "holdout_start": args.holdout_start,
              "data_from": str(min(df.index[0] for df in frames.values()).date()),
              "data_to": str(max(df.index[-1] for df in frames.values()).date())}

    if args.holdout_start:
        cut = pd.Timestamp(args.holdout_start)
        res_in = run_backtest(frames, p, BtConfig(initial_equity=args.equity))
        # in-sample = equity curve before the cut
        ins = res_in["equity"][res_in["equity"].index < cut]
        in_stats = stats(ins, [t for t in res_in["trades"] if t.exit_time < cut])
        print("\n=== In-sample (before holdout; fine to study) ===")
        for k, v in in_stats.items():
            print(f"  {k}: {v}")
        ho = run_backtest(frames, p, BtConfig(initial_equity=args.equity, trading_enabled_from=cut))
        show(f"HOLDOUT from {cut.date()} (look once, do not re-tune on this)", ho)
        final = ho
        report["in_sample"] = in_stats
    else:
        final = run_backtest(frames, p, base)
        show("Full period (no holdout set: treat as in-sample)", final)
    report["evaluated"] = final["stats"]
    report["equity"] = _downsample(final["equity"])
    report["trade_returns"] = [round(t.ret_on_equity, 6) for t in final["trades"]]

    print("\n=== Monte Carlo (bootstrapped trade order) ===")
    report["monte_carlo"] = monte_carlo(final["trades"])
    for k, v in report["monte_carlo"].items():
        print(f"  {k}: {v}")

    report["sensitivity"] = []
    if not args.no_grid:
        print("\n=== Parameter sensitivity (full period) ===  sma/mom/atr_mult -> CAGR%, maxDD%, trades")
        for sma, mom, mult in [(50, 60, 2.5), (100, 90, 3.0), (150, 120, 3.0), (200, 180, 3.5), (100, 90, 2.0)]:
            r = run_backtest(frames, TrendParams(sma_len=sma, mom_len=mom, atr_stop_mult=mult,
                                                 risk_pct=args.risk_pct), base)["stats"]
            report["sensitivity"].append({"sma": sma, "mom": mom, "atr_mult": mult, **r})
            print(f"  {sma:>3}/{mom:>3}/{mult}: CAGR {r.get('cagr_pct')}%  DD {r.get('max_drawdown_pct')}%  trades {r.get('n_trades')}")

    out = ROOT / "data" / "fx_trend_backtest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, default=float))
    print(f"\nReport saved to {out} (shown on the admin dashboard's Backtest tab).")

    print("\nReminder: a good backtest is necessary, not sufficient. Demo-trade 2-3 months and "
          "compare live fills to the assumed costs before risking real money.")


if __name__ == "__main__":
    main()
