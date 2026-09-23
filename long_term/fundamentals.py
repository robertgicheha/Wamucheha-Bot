"""
Fundamentals fetcher. Free by default (yfinance, no API key/signup needed) with
optional richer paid-tier fallbacks (Financial Modeling Prep, Alpha Vantage),
plus real NSE Kenya fundamentals via the free afx.kwayisi.org quote source.

Source priority per ticker:
  - NSE tickers (market="nse", or found in NSEFeed's known ticker list):
    afx.kwayisi.org via NSEFeed.get_quote() — the only free source found
    that has real NSE fundamentals (P/E, dividend yield, market cap, EPS).
  - Everything else (US stocks/ETFs): yfinance first (free, no key), then
    FMP or Alpha Vantage if the corresponding API key is configured and
    yfinance didn't return usable data.
"""
import os
import time
import logging
import requests

logger = logging.getLogger("fundamentals")

try:
    import yfinance as yf
    _has_yfinance = True
except ImportError:
    _has_yfinance = False


class FundamentalsFetcher:
    def __init__(self, fmp_api_key: str = None, alpha_vantage_key: str = None, nse_feed=None):
        self.fmp_key = fmp_api_key or os.environ.get("FMP_API_KEY")
        self.av_key = alpha_vantage_key or os.environ.get("ALPHA_VANTAGE_KEY")
        self._nse_feed = nse_feed
        self._cache = {}          # (ticker, market) -> (fetched_at, profile)
        self._cache_ttl = 6 * 3600
        self._kes_rate = (0.0, None)

    def kes_per_usd(self) -> float:
        """Live USD/KES rate (yfinance KES=X), cached 6h; falls back to ~129."""
        fetched_at, rate = self._kes_rate
        if rate and time.time() - fetched_at < self._cache_ttl:
            return rate
        rate = None
        if _has_yfinance:
            try:
                closes = yf.Ticker("KES=X").history(period="5d")["Close"].dropna()
                rate = float(closes.iloc[-1]) if len(closes) else None
            except Exception as e:
                logger.warning(f"KES rate fetch failed: {e}")
        rate = rate or 129.0
        self._kes_rate = (time.time(), rate)
        return rate

    @property
    def nse_feed(self):
        if self._nse_feed is None:
            from data_feeds.nse_feed import NSEFeed
            self._nse_feed = NSEFeed()
        return self._nse_feed

    def get_profile(self, ticker: str, market: str = None) -> dict | None:
        """Returns a normalized dict: market_cap, pe_ratio, dividend_yield,
        payout_ratio, dividend_growth_years, revenue_growth_pct, peg_ratio,
        debt_to_equity, sector. Any field this source can't supply is None —
        long_term/screener.py already skips checks where the field is None,
        so a thinner NSE profile still screens on whatever it does have."""
        key = (ticker.upper(), market)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self._cache_ttl:
            return cached[1]
        profile = self._fetch_profile(ticker, market)
        if profile:
            self._cache[key] = (time.time(), profile)
        return profile

    def _fetch_profile(self, ticker: str, market: str = None) -> dict | None:
        # market=None keeps the old auto-detect; callers pass "us" explicitly
        # because some symbols (e.g. GLD) exist on both the NSE and NYSE.
        if market == "nse" or (market is None and self._is_nse_ticker(ticker)):
            return self._from_nse(ticker)

        if _has_yfinance:
            profile = self._from_yfinance(ticker)
            if profile:
                return profile

        if self.fmp_key:
            return self._from_fmp(ticker)
        if self.av_key:
            return self._from_alpha_vantage(ticker)
        return None

    def _is_nse_ticker(self, ticker: str) -> bool:
        from data_feeds.nse_feed import DEFAULT_NSE_TICKERS
        return ticker.upper() in DEFAULT_NSE_TICKERS

    # ---------- NSE (afx.kwayisi.org, free) ----------

    def _from_nse(self, ticker: str) -> dict | None:
        quote = self.nse_feed.get_quote(ticker)
        if not quote or quote.get("price") is None:
            return None

        payout_ratio = None
        eps, dps = quote.get("eps"), quote.get("dividend_per_share")
        if eps and eps > 0 and dps is not None:
            payout_ratio = dps / eps * 100

        kes = self.kes_per_usd()
        market_cap = quote.get("market_cap")
        eps = quote.get("eps")
        return {
            "ticker": ticker,
            "name": quote.get("name"),
            "market": "nse",
            "quote_type": "ETF" if ticker.upper() in ("GLD", "SMWF") else "EQUITY",
            "currency": "KES",
            "price": quote.get("price"),
            "price_usd": quote["price"] / kes if quote.get("price") else None,
            "market_cap": market_cap,                       # KES
            "market_cap_usd": market_cap / kes if market_cap else None,
            "kes_per_usd": kes,
            "pe_ratio": quote.get("pe_ratio"),
            "earnings_yield_pct": (eps / quote["price"] * 100) if eps and quote.get("price") else None,
            "eps": eps,
            "dividend_per_share": quote.get("dividend_per_share"),
            "shares_outstanding": quote.get("shares_outstanding"),
            "peg_ratio": None,  # not available from this free source
            "revenue_growth_pct": None,
            "dividend_yield": quote.get("dividend_yield"),
            "payout_ratio": payout_ratio,
            "dividend_growth_years": None,  # afx.kwayisi.org has no dividend history series
            "debt_to_equity": None,
            "sector": None,
            # Price momentum straight from the quote page (no history needed)
            "return_1w": quote.get("return_1w"),
            "return_3m": quote.get("return_3m"),
            "return_6m": quote.get("return_6m"),
            "return_1y": quote.get("return_1y"),
            "return_ytd": quote.get("return_ytd"),
            "day_change_pct": quote.get("change_pct"),
            "source": "afx.kwayisi.org",
        }

    # ---------- yfinance (free, no key) ----------

    def _from_yfinance(self, ticker: str) -> dict | None:
        try:
            info = yf.Ticker(ticker).info
        except Exception as e:
            logger.warning(f"yfinance fetch failed for {ticker}: {e}")
            return None

        if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            return None

        payout = info.get("payoutRatio")
        rev_growth = info.get("revenueGrowth")
        pct = lambda v: v * 100 if v is not None else None  # yfinance fractions -> %
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        target = info.get("targetMeanPrice")
        quote_type = info.get("quoteType", "EQUITY")

        profile = {
            "ticker": ticker,
            "name": info.get("longName") or info.get("shortName"),
            "market": "us",
            "quote_type": quote_type,
            "currency": info.get("currency", "USD"),
            "price": price,
            "price_usd": price,
            "market_cap": info.get("marketCap"),
            "market_cap_usd": info.get("marketCap"),
            "enterprise_value": info.get("enterpriseValue"),
            "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
            "forward_pe": info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"),
            "ps_ratio": info.get("priceToSalesTrailing12Months"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "peg_ratio": info.get("pegRatio") or info.get("trailingPegRatio"),
            "eps": info.get("trailingEps"),
            "book_value_per_share": info.get("bookValue"),
            "roe_pct": pct(info.get("returnOnEquity")),
            "roa_pct": pct(info.get("returnOnAssets")),
            "profit_margin_pct": pct(info.get("profitMargins")),
            "operating_margin_pct": pct(info.get("operatingMargins")),
            "gross_margin_pct": pct(info.get("grossMargins")),
            "revenue": info.get("totalRevenue"),
            "net_income": info.get("netIncomeToCommon"),
            "free_cash_flow": info.get("freeCashflow"),
            "revenue_growth_pct": pct(rev_growth),
            "earnings_growth_pct": pct(info.get("earningsGrowth")),
            # yfinance already reports dividendYield in percent (1.99 == 1.99%)
            "dividend_yield": info.get("dividendYield") or (info.get("yield") or 0) * 100 or None,
            "dividend_per_share": info.get("dividendRate"),
            "five_year_avg_dividend_yield": info.get("fiveYearAvgDividendYield"),
            "payout_ratio": payout * 100 if payout is not None else None,
            "dividend_growth_years": (self._dividend_growth_years_yfinance(ticker)
                                      if quote_type == "EQUITY" else None),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "beta": info.get("beta"),
            "week52_high": info.get("fiftyTwoWeekHigh"),
            "week52_low": info.get("fiftyTwoWeekLow"),
            "analyst_target": target,
            "analyst_upside_pct": (target / price - 1) * 100 if target and price else None,
            "analyst_rating": info.get("recommendationKey"),
            "analyst_count": info.get("numberOfAnalystOpinions"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "source": "yfinance",
        }
        if quote_type == "ETF":
            profile.update({
                "total_assets": info.get("totalAssets"),
                "expense_ratio_pct": info.get("netExpenseRatio"),
                "return_ytd": info.get("ytdReturn"),
                "return_3y_avg": pct(info.get("threeYearAverageReturn")),
                "return_5y_avg": pct(info.get("fiveYearAverageReturn")),
            })
        return profile

    def _dividend_growth_years_yfinance(self, ticker: str) -> int | None:
        """Count consecutive trailing years of higher annual dividends, from
        yfinance's actual dividend payment history (not a paid endpoint —
        this is the field long_term/screener.py's min_dividend_years_growth
        check needs, which neither the FMP nor Alpha Vantage path below ever
        actually populated)."""
        try:
            dividends = yf.Ticker(ticker).dividends
        except Exception:
            return None
        if dividends is None or len(dividends) == 0:
            return None

        annual = dividends.groupby(dividends.index.year).sum().sort_index()

        # Drop the current calendar year if it's still in progress — its
        # partial-year total is naturally lower than a completed prior year
        # and would otherwise look like a cut, understating the real streak.
        from datetime import datetime as _dt
        current_year = _dt.now().year
        if len(annual) and annual.index[-1] == current_year:
            annual = annual.iloc[:-1]

        if len(annual) < 2:
            return 0

        years = 0
        vals = annual.values
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] > vals[i - 1]:
                years += 1
            else:
                break
        return years

    # ---------- Financial Modeling Prep (optional, paid tiers beyond free) ----------

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
                "dividend_growth_years": self._dividend_growth_years_fmp(ticker),
                "debt_to_equity": r.get("debtEquityRatioTTM"),
                "sector": profile.get("sector"),
                "source": "fmp",
            }
        except Exception:
            return None

    def _dividend_growth_years_fmp(self, ticker: str) -> int | None:
        try:
            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/stock_dividend/{ticker}"
            resp = requests.get(url, params={"apikey": self.fmp_key}, timeout=15)
            resp.raise_for_status()
            hist = resp.json().get("historical", [])
            if not hist:
                return None
            from collections import defaultdict
            annual = defaultdict(float)
            for h in hist:
                year = h.get("date", "")[:4]
                if year:
                    annual[year] += float(h.get("adjDividend") or h.get("dividend") or 0)
            years_sorted = sorted(annual.keys(), reverse=True)
            vals = [annual[y] for y in years_sorted]
            count = 0
            for i in range(len(vals) - 1):
                if vals[i] > vals[i + 1]:
                    count += 1
                else:
                    break
            return count
        except Exception:
            return None

    # ---------- Alpha Vantage (optional, 25 req/day free tier) ----------

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
                "dividend_growth_years": None,  # OVERVIEW endpoint has no dividend history series
                "debt_to_equity": None,  # not provided by OVERVIEW endpoint
                "sector": d.get("Sector"),
                "source": "alpha_vantage",
            }
        except Exception:
            return None
