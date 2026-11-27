"""
Command-line side of the Stocks & ETFs module.

  python -m scripts.run_stock_analysis                 # both markets
  python -m scripts.run_stock_analysis --market nse    # fetches NSE prices and records today's close
  python -m scripts.run_stock_analysis --import-csv history.csv   # load older NSE closes

NSE history only grows on days when prices are fetched. Schedule the --market nse form on
weekdays after 15:30 East Africa Time (Task Scheduler or cron) so no day is missed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from stock_analysis import history, service  # noqa: E402

LISTS = (("best_buy", "Best buy"), ("best_stocks", "Best stocks"), ("likely_to_rise", "Likely to rise"),
         ("speculative", "Speculative (high risk)"), ("dividends", "Top dividend yields"))


def report(market: str) -> bool:
    view = service.market_view(market)
    meta = view["meta"]
    print(f"\n=== {meta['label']} | {meta['source']} | {meta['updated'] or 'no data'} ===")
    if meta["note"]:
        print(meta["note"])
    if not view["rows"]:
        return False
    print(view["insights"]["summary"])
    if market == "nse":
        print(f"Recorded history: {meta['history_days']} trading day(s)")
    for key, title in LISTS:
        found = view["picks"][key][:5]
        names = [x["symbol"] if isinstance(x, dict) else x for x in found]
        print(f"{title:24s} {', '.join(names) or '-'}")
    print(view["disclaimer"])
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="NSE and US stock/ETF research report")
    p.add_argument("--market", choices=["nse", "us", "all"], default="all")
    p.add_argument("--import-csv", metavar="FILE", help="load NSE history (date,symbol,close[,volume])")
    args = p.parse_args()
    if args.import_csv:
        try:
            print(f"Imported {history.import_csv(args.import_csv)} NSE closes.")
        except (OSError, ValueError) as e:
            print(f"Import failed: {e}")
            return 1
        return 0
    markets = ["nse", "us"] if args.market == "all" else [args.market]
    return 0 if all([report(m) for m in markets]) else 1


if __name__ == "__main__":
    sys.exit(main())
