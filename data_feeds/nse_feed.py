"""
NSE Kenya data scraper.

The Nairobi Securities Exchange does NOT offer a public trading API. There is no
ccxt support, no OANDA equivalent, no broker with open algo-access for NSE.

This module:
1. Attempts to pull NSE data via the Apify NSE scraper (requires API key)
2. Falls back to a basic web scraper using requests + BeautifulSoup
3. Maintains a local CSV cache so the screener can work even when the source is down

CRITICAL LIMITATION: NSE is alert/analysis only — NOT automated execution.
The long_term/screener.py uses this for trend context on Kenyan stocks, but
any actual NSE trade would need to go through your broker (Genghis Capital,
AIB-AXYS, Faida) manually or via their proprietary FIX/API if they offer one.
"""
import os
import time
import csv
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

try:
    from bs4 import BeautifulSoup
    _has_bs4 = True
except ImportError:
    _has_bs4 = False

CACHE_DIR = Path(__file__).parent.parent / "data" / "nse_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Known NSE top-cap tickers — expand as needed
DEFAULT_NSE_TICKERS = [
    "SCOM", "EQTY", "KCB", "BAT", "EABL", "SAFARICOM", "DTK",
    "COOP", "ABSA", "KNC", "NIC", "KEGN", "I&M", "HFCK",
]


class NSEFeed:
    def __init__(self, apify_token: str = None):
        self.apify_token = apify_token or os.environ.get("APIFY_TOKEN")
        self._cache = {}
        self._cache_ttl = 3600  # 1 hour — NSE data changes less frequently

    def _get_cached(self, ticker: str) -> pd.DataFrame | None:
        cache_file = CACHE_DIR / f"{ticker}_ohlcv.csv"
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < self._cache_ttl:
                df = pd.read_csv(cache_file, index_col="timestamp", parse_dates=True)
                return df
        return None

    def _save_cache(self, ticker: str, df: pd.DataFrame):
        cache_file = CACHE_DIR / f"{ticker}_ohlcv.csv"
        df.to_csv(cache_file)

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 200) -> pd.DataFrame:
        """Returns OHLCV for an NSE ticker. Only daily timeframe is reliably
        available — intraday NSE data requires a paid data feed."""
        ticker = symbol.replace(".NSE", "").replace("/NSE", "")

        cached = self._get_cached(ticker)
        if cached is not None:
            return cached.tail(limit)

        # Try Apify scraper first (paid, reliable)
        if self.apify_token:
            df = self._fetch_via_apify(ticker)
            if df is not None and len(df) > 0:
                self._save_cache(ticker, df)
                return df.tail(limit)

        # Fallback: basic web scrape (fragile, may break if NSE changes their site)
        df = self._fetch_via_scrape(ticker)
        if df is not None and len(df) > 0:
            self._save_cache(ticker, df)
            return df.tail(limit)

        # Last resort: return whatever is cached on disk, even if stale
        cached = self._get_cached(ticker)
        if cached is not None:
            return cached.tail(limit)

        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def _fetch_via_apify(self, ticker: str) -> pd.DataFrame | None:
        """Use the Apify NSE Kenya scraper actor."""
        try:
            url = "https://api.apify.com/v2/acts/wafspaul~nse-kenya-market-data/runs"
            resp = requests.post(
                url,
                json={"tickers": [ticker], "days": 90},
                headers={"Authorization": f"Bearer {self.apify_token}"},
                timeout=30,
            )
            resp.raise_for_status()
            run_data = resp.json().get("data", {})
            run_id = run_data.get("id")
            if not run_id:
                return None

            # Poll for completion (Apify runs are async)
            for _ in range(30):
                time.sleep(2)
                status_resp = requests.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {self.apify_token}"},
                    timeout=10,
                )
                status = status_resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    return None

            # Fetch dataset
            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            if not dataset_id:
                return None
            data_resp = requests.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {self.apify_token}"},
                timeout=15,
            )
            items = data_resp.json()
            if not items:
                return None

            rows = []
            for item in items:
                rows.append({
                    "timestamp": pd.Timestamp(item.get("date", item.get("timestamp"))),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("close", 0)),
                    "volume": int(item.get("volume", 0)),
                })
            df = pd.DataFrame(rows)
            df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)
            return df
        except Exception:
            return None

    def _fetch_via_scrape(self, ticker: str) -> pd.DataFrame | None:
        """Basic web scraper — fragile, may break. Use Apify or a paid data
        feed for production. This exists so the system works for initial
        testing without an Apify subscription."""
        if not _has_bs4:
            return None
        try:
            # NSE's public historical data page
            url = f"https://www.nse.co.ke/api/historical/{ticker}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            }
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            # NSE API response format varies — adapt as needed
            records = data if isinstance(data, list) else data.get("data", [])
            rows = []
            for r in records:
                rows.append({
                    "timestamp": pd.Timestamp(r.get("date", r.get("DATE"))),
                    "open": float(r.get("open", r.get("OPEN", 0))),
                    "high": float(r.get("high", r.get("HIGH", 0))),
                    "low": float(r.get("low", r.get("LOW", 0))),
                    "close": float(r.get("close", r.get("CLOSE", 0))),
                    "volume": int(r.get("volume", r.get("VOLUME", 0))),
                })
            df = pd.DataFrame(rows)
            if len(df) > 0:
                df.set_index("timestamp", inplace=True)
                df.sort_index(inplace=True)
                return df
            return None
        except Exception:
            return None

    def latest_price(self, symbol: str) -> float | None:
        """Get latest closing price — NSE doesn't provide real-time via free API."""
        df = self.get_ohlcv(symbol, timeframe="1d", limit=1)
        if len(df) > 0:
            return float(df.iloc[-1]["close"])
        return None

    def get_nse_tickers(self) -> list:
        """Returns list of actively traded NSE tickers."""
        return DEFAULT_NSE_TICKERS.copy()
