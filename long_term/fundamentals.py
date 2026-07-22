"""
Fundamentals fetcher. Wraps Alpha Vantage / Financial Modeling Prep so screener
logic doesn't care which vendor's JSON shape it's dealing with.

Note on Kenyan (NSE) stocks: neither Alpha Vantage nor FMP has reliable NSE
fundamentals coverage. For NSE names, this falls back to manual/CSV input —
see docs in long_term/README-ish comment below. Realistically, budget for
manually maintaining a small watchlist CSV for NSE stocks rather than expecting
full API coverage there.
"""
import os
import requests


class FundamentalsFetcher:
    def __init__(self, fmp_api_key: str = None, alpha_vantage_key: str = None):
        self.fmp_key = fmp_api_key or os.environ.get("FMP_API_KEY")
        self.av_key = alpha_vantage_key or os.environ.get("ALPHA_VANTAGE_KEY")

    def get_profile(self, ticker: str) -> dict | None:
        """Returns a normalized dict: market_cap, pe_ratio, dividend_yield,
        payout_ratio, dividend_years_growth (approximated), debt_to_equity."""
        if self.fmp_key:
            return self._from_fmp(ticker)
        if self.av_key:
            return self._from_alpha_vantage(ticker)
        return None

    def _from_fmp(self, ticker: str) -> dict | None:
        try:
            url = f"https://financialmodelingprep.com/api/v3/ratios-ttm/{ticker}"
            resp = requests.get(url, params={"apikey": self.fmp_key}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            r = data[0]

            profile_url = f"https://financialmodelingprep.com/api/v3/profile/{ticker}"
            profile_resp = requests.get(profile_url, params={"apikey": self.fmp_key}, timeout=15)
            profile = profile_resp.json()[0] if profile_resp.ok and profile_resp.json() else {}

            # revenue growth: pull latest 2 annual income statements and compute YoY
            revenue_growth_pct = None
            try:
                inc_url = f"https://financialmodelingprep.com/api/v3/income-statement/{ticker}"
                inc_resp = requests.get(inc_url, params={"apikey": self.fmp_key, "limit": 2}, timeout=15)
                inc = inc_resp.json()
                if inc and len(inc) >= 2 and inc[1]["revenue"]:
                    revenue_growth_pct = (inc[0]["revenue"] - inc[1]["revenue"]) / inc[1]["revenue"] * 100
            except Exception:
                pass

            pe = r.get("peRatioTTM")
            # PEG = P/E divided by earnings growth rate; use revenue growth as a proxy
            # when EPS growth isn't directly available from this endpoint.
            peg = (pe / revenue_growth_pct) if (pe and revenue_growth_pct and revenue_growth_pct > 0) else None

            return {
                "ticker": ticker,
                "market_cap": profile.get("mktCap"),
                "pe_ratio": pe,
                "peg_ratio": peg,
                "revenue_growth_pct": revenue_growth_pct,
                "dividend_yield": r.get("dividendYielPercentageTTM") or r.get("dividendYielTTM"),
                "payout_ratio": r.get("payoutRatioTTM"),
                "debt_to_equity": r.get("debtEquityRatioTTM"),
                "sector": profile.get("sector"),
            }
        except Exception:
            return None

    def _from_alpha_vantage(self, ticker: str) -> dict | None:
        try:
            url = "https://www.alphavantage.co/query"
            resp = requests.get(url, params={
                "function": "OVERVIEW", "symbol": ticker, "apikey": self.av_key,
            }, timeout=15)
            resp.raise_for_status()
            d = resp.json()
            if not d or "Symbol" not in d:
                return None

            def to_float(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            payout = to_float(d.get("PayoutRatio"))
            return {
                "ticker": ticker,
                "market_cap": to_float(d.get("MarketCapitalization")),
                "pe_ratio": to_float(d.get("PERatio")),
                "peg_ratio": to_float(d.get("PEGRatio")),
                "revenue_growth_pct": to_float(d.get("QuarterlyRevenueGrowthYOY")) * 100
                    if to_float(d.get("QuarterlyRevenueGrowthYOY")) is not None else None,
                "dividend_yield": to_float(d.get("DividendYield")),
                "payout_ratio": payout * 100 if payout is not None else None,
                "debt_to_equity": None,  # not provided by OVERVIEW endpoint
                "sector": d.get("Sector"),
            }
        except Exception:
            return None
