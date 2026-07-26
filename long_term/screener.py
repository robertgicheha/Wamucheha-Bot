"""
Long-term equity screener with transparent, disclosed rules.

Two kinds of output:
1. `screen_universe()` — batch pass/fail against config rules.
2. `trend_context()` — technical context for fundamentally-good names.
"""
import pandas as pd
from long_term.fundamentals import FundamentalsFetcher


class EquityScreener:
    def __init__(self, config: dict, fundamentals: FundamentalsFetcher, market_data_fn):
        self.cfg = config["long_term"]["screens"]
        self.fundamentals = fundamentals
        self.market_data_fn = market_data_fn

    def screen_one(self, ticker: str) -> dict | None:
        profile = self.fundamentals.get_profile(ticker)
        if not profile:
            return None

        reasons_pass = []
        reasons_fail = []

        mc = profile.get("market_cap")
        if mc is not None:
            if mc >= self.cfg["min_market_cap_usd"]:
                reasons_pass.append(f"Market cap ${mc/1e9:.1f}B clears the "
                                     f"${self.cfg['min_market_cap_usd']/1e9:.1f}B floor")
            else:
                reasons_fail.append(f"Market cap ${mc/1e9:.2f}B below floor")

        payout = profile.get("payout_ratio")
        if payout is not None:
            if payout <= self.cfg["max_payout_ratio"]:
                reasons_pass.append(f"Payout ratio {payout:.0f}% — sustainable")
            else:
                reasons_fail.append(f"Payout ratio {payout:.0f}% is high")

        div_yield = profile.get("dividend_yield")
        if div_yield:
            reasons_pass.append(f"Dividend yield {div_yield:.2f}%")

        # Dividend growth years check
        div_growth_years = profile.get("dividend_growth_years")
        min_div_years = self.cfg.get("min_dividend_years_growth")
        if div_growth_years is not None and min_div_years is not None:
            if div_growth_years >= min_div_years:
                reasons_pass.append(f"Dividend growing for {div_growth_years} years (>= {min_div_years})")
            else:
                reasons_fail.append(f"Dividend growth only {div_growth_years} years (< {min_div_years})")

        pe = profile.get("pe_ratio")
        if pe:
            reasons_pass.append(f"P/E {pe:.1f}")

        peg = profile.get("peg_ratio")
        if peg is not None:
            if peg < 1.5:
                reasons_pass.append(f"PEG {peg:.2f} — growth reasonably priced")
            else:
                reasons_pass.append(f"PEG {peg:.2f} — richly priced")

        rev_growth = profile.get("revenue_growth_pct")
        if rev_growth is not None:
            if rev_growth > 0:
                reasons_pass.append(f"Revenue growth +{rev_growth:.1f}% YoY")
            else:
                reasons_fail.append(f"Revenue growth {rev_growth:.1f}% YoY — shrinking")

        passed = len(reasons_fail) == 0 and len(reasons_pass) > 0

        return {
            "ticker": ticker,
            "passed": passed,
            "profile": profile,
            "reasons_pass": reasons_pass,
            "reasons_fail": reasons_fail,
        }

    def screen_universe(self, tickers: list) -> list:
        results = [self.screen_one(t) for t in tickers]
        return [r for r in results if r is not None]

    def trend_context(self, ticker: str) -> dict | None:
        df = self.market_data_fn(ticker)
        if df is None or len(df) < 200:
            return None

        df = df.copy()
        df["ma50"] = df["close"].rolling(50).mean()
        df["ma200"] = df["close"].rolling(200).mean()
        last = df.iloc[-1]

        above_200 = last["close"] > last["ma200"]
        golden_cross = last["ma50"] > last["ma200"]
        momentum_30d = (last["close"] / df["close"].iloc[-30] - 1) * 100 if len(df) >= 30 else None

        return {
            "ticker": ticker,
            "above_200dma": bool(above_200),
            "golden_cross": bool(golden_cross),
            "momentum_30d_pct": round(momentum_30d, 2) if momentum_30d is not None else None,
            "note": "Trend context only — not a prediction.",
        }

    def format_alert(self, screen_result: dict, trend: dict | None = None,
                      news: dict | None = None) -> str:
        lines = [f"{screen_result['ticker']}: {'PASSED' if screen_result['passed'] else 'did not pass'} screen"]
        for r in screen_result["reasons_pass"]:
            lines.append(f"  + {r}")
        for r in screen_result["reasons_fail"]:
            lines.append(f"  - {r}")
        if trend:
            lines.append(f"  Trend: {'above' if trend['above_200dma'] else 'below'} 200DMA, "
                          f"{'golden cross' if trend['golden_cross'] else 'no golden cross'}, "
                          f"30d momentum {trend.get('momentum_30d_pct', 'N/A')}%")
        if news and news.get("avg_sentiment") is not None:
            lines.append(f"  News: {news['label']} ({news['avg_sentiment']:+.2f})")
        return "\n".join(lines)

