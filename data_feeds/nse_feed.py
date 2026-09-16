"""
NSE Kenya data scraper.

The Nairobi Securities Exchange does NOT offer a public trading API. There is no
ccxt support, no OANDA equivalent, no broker with open algo-access for NSE.

Data sources, in priority order:
1. Apify NSE scraper (paid, requires APIFY_TOKEN) — used for get_ohlcv() when
   available, since it's the only source here that returns real historical
   daily OHLCV series.
2. afx.kwayisi.org (free, no key, no signup) — a live NSE quote aggregator.
   Verified working against real requests: the exchange-wide page
   (afx.kwayisi.org/nse/) exposes the full listed-companies table plus
   ready-made Top Gainers / Bottom Losers tables in ONE request, and each
   ticker's page (afx.kwayisi.org/nse/<ticker>/) exposes price, day
   low/high, volume, and fundamentals (EPS, P/E, dividend yield, market
   cap) — this is what get_market_snapshot() and get_quote() use, and it's
   also what fills the fundamentals gap noted in long_term/fundamentals.py.
   It does NOT expose a historical daily-close time series though (only a
   live snapshot + 1WK/4WK/3MO % performance), so it cannot alone support a
   200-day-moving-average trend check — see _accumulate_daily_snapshot().
3. Local CSV cache, including a slow-building one built by appending one
   snapshot row per calendar day (see _accumulate_daily_snapshot) so
   trend_context-style analysis becomes possible for free after enough
   days have accumulated, without ever promising it's available on day one.

CRITICAL LIMITATION: NSE is alert/analysis only — NOT automated execution.
The long_term/screener.py uses this for trend context on Kenyan stocks, but
any actual NSE trade would need to go through your broker (Genghis Capital,
AIB-AXYS, Faida) manually or via their proprietary FIX/API if they offer one.
"""
import os
import re
import time
import logging
from typing import TYPE_CHECKING, Any

import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

if TYPE_CHECKING:
    # Always visible to the type checker, regardless of whether bs4 is
    # actually installed at runtime — avoids "possibly unbound" errors on
    # every use of BeautifulSoup below without needing # type: ignore.
    from bs4 import BeautifulSoup
    _has_bs4 = True
else:
    try:
        from bs4 import BeautifulSoup
        _has_bs4 = True
    except ImportError:
        _has_bs4 = False

logger = logging.getLogger("nse_feed")

CACHE_DIR = Path(__file__).parent.parent / "data" / "nse_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

AFX_BASE = "https://afx.kwayisi.org/nse"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# Every security listed on the NSE, verified against the live
# afx.kwayisi.org/nse/ listed-companies table (71 tickers, including the two
# KPLC preference-share lines). Equities, REITs, and the two NSE-listed ETFs
# (GLD, SMWF) are all included — "on the exchange" isn't limited to common
# stock. This is a point-in-time snapshot, not a live feed: the NSE lists and
# delists names over time (e.g. AMAC/SKL are recent additions), so treat this
# as a good default rather than a permanently authoritative list — the true
# current universe is always available live via get_market_snapshot()["universe"].
DEFAULT_NSE_TICKERS = [
    "ABSA", "ALP", "AMAC", "ARM", "BAMB", "BAT", "BKG", "BOC", "BRIT",
    "CABL", "CARB", "CGEN", "CIC", "COOP", "CRWN", "CTUM", "DCON", "DTK",
    "EABL", "EGAD", "EQTY", "EVRD", "FMLY", "FTGH", "GLD", "HAFR", "HBE",
    "HFCB", "IMH", "JUB", "KAPC", "KCB", "KEGN", "KNRE", "KPC", "KPLC",
    "KPLC-P4", "KPLC-P7", "KQ", "KUKZ", "KURV", "LAPR", "LBTY", "LIMT",
    "LKL", "MSC", "NBV", "NCBA", "NMG", "NSE", "OCH", "PORT", "SASN",
    "SBIC", "SCAN", "SCBK", "SCOM", "SGL", "SKL", "SLAM", "SMER", "SMWF",
    "TCL", "TOTL", "TPSE", "TRFC", "UCHM", "UMME", "UNGA", "WTK", "XPRS",
]


def _parse_number_suffix(s: str | None) -> float | None:
    """Parse values like '3.68M', '1.46T', '1,137', '36.50' into a float."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = re.match(r"^([+-]?[\d.]+)\s*([KMBT]?)$", s, re.I)
    if not m:
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    val = float(m.group(1))
    mult = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(m.group(2).upper(), 1)
    return val * mult


def _parse_pct(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _kv_table(table: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            out[tds[0].get_text(strip=True)] = tds[1].get_text(strip=True)
    return out


class NSEFeed:
    def __init__(self, apify_token: str | None = None):
        self.apify_token = apify_token or os.environ.get("APIFY_TOKEN")
        self._snapshot_cache = None
        self._snapshot_cache_ts = 0
        self._snapshot_cache_ttl = 900  # 15 min — index page is cheap, refresh often
        self._quote_cache = {}
        self._quote_cache_ttl = 900

    # ---------- free live snapshot (afx.kwayisi.org) ----------

    def get_market_snapshot(self, force_refresh: bool = False) -> dict[str, Any] | None:
        """One request covering the whole exchange: NASI index, ready-made
        Top Gainers / Bottom Losers, and the full listed-companies table
        (ticker, name, volume, price, day change). Returns None if the
        source is unreachable (caller should fall back to cache/skip)."""
        now = time.time()
        if not force_refresh and self._snapshot_cache is not None \
                and now - self._snapshot_cache_ts < self._snapshot_cache_ttl:
            return self._snapshot_cache

        if not _has_bs4:
            logger.warning("beautifulsoup4 not installed — cannot scrape NSE snapshot")
            return self._snapshot_cache

        try:
            resp = requests.get(f"{AFX_BASE}/", headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            logger.warning(f"NSE snapshot fetch failed: {e}")
            return self._snapshot_cache

        tables = soup.find_all("table")
        if len(tables) < 4:
            logger.warning(f"NSE snapshot page layout unexpected — got {len(tables)} tables")
            return self._snapshot_cache

        def parse_movers(table):
            out = []
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                a = tds[0].find("a")
                if not a:
                    continue
                out.append({
                    "ticker": a.get_text(strip=True),
                    "price": _parse_number_suffix(tds[1].get_text(strip=True)),
                    "change_pct": _parse_pct(tds[2].get_text(strip=True)),
                })
            return out

        def parse_universe(table):
            out = []
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                ticker = tds[0].get_text(strip=True)
                if not ticker:
                    continue
                change_cls = tds[4].get("class") or []
                out.append({
                    "ticker": ticker,
                    "name": tds[1].get_text(strip=True),
                    "volume": _parse_number_suffix(tds[2].get_text(strip=True)),
                    "price": _parse_number_suffix(tds[3].get_text(strip=True)),
                    "change": _parse_number_suffix(tds[4].get_text(strip=True)),
                    "direction": "up" if "hi" in change_cls else ("down" if "lo" in change_cls else "flat"),
                })
            return out

        snapshot = {
            "gainers": parse_movers(tables[1]),
            "losers": parse_movers(tables[2]),
            "universe": parse_universe(tables[3]),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "afx.kwayisi.org",
        }
        self._snapshot_cache = snapshot
        self._snapshot_cache_ts = now
        return snapshot

    def get_quote(self, ticker: str, force_refresh: bool = False) -> dict[str, Any] | None:
        """Live quote + fundamentals for one NSE ticker from its afx.kwayisi.org
        detail page. Returns None for an unknown ticker or on fetch failure."""
        ticker = ticker.upper().strip()
        now = time.time()
        if not force_refresh and ticker in self._quote_cache:
            ts, data = self._quote_cache[ticker]
            if now - ts < self._quote_cache_ttl:
                return data

        if not _has_bs4:
            return None

        try:
            resp = requests.get(f"{AFX_BASE}/{ticker.lower()}/", headers=_HEADERS, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            logger.warning(f"NSE quote fetch failed for {ticker}: {e}")
            return None

        h1 = soup.find("h1")
        name = h1.get_text(strip=True).split(" - ", 1)[-1] if h1 else ticker

        h2div = soup.find("div", class_="h2")
        price, change, change_pct = None, None, None
        if h2div:
            full_text = h2div.get_text(" ", strip=True)
            # e.g. "SCOM • 36.50 ▴ 0.65 (1.81%) 19 minutes ago" —
            # the price is the first decimal number (ticker/bullet aren't numeric).
            price_m = re.search(r"(\d[\d,]*\.\d+)", full_text)
            if price_m:
                price = float(price_m.group(1).replace(",", ""))
            move_span = h2div.find("span", class_=re.compile(r"^(hi|lo)$"))
            if move_span:
                m = re.search(r"([\d.]+)\s*\(([-\d.]+)%\)", move_span.get_text(strip=True))
                if m:
                    sign = -1 if "lo" in (move_span.get("class") or []) else 1
                    change = sign * float(m.group(1))
                    change_pct = sign * abs(float(m.group(2)))

        tables = soup.find_all("table")
        trading = _kv_table(tables[0]) if len(tables) > 0 else {}
        valuation = _kv_table(tables[1]) if len(tables) > 1 else {}

        quote = {
            "ticker": ticker,
            "name": name,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "day_low": _parse_number_suffix(trading.get("Day’s Low Price") or trading.get("Days Low Price")),
            "day_high": _parse_number_suffix(trading.get("Day’s High Price") or trading.get("Days High Price")),
            "volume": _parse_number_suffix(trading.get("Traded Volume")),
            "eps": _parse_number_suffix(valuation.get("Earnings Per Share")),
            "pe_ratio": _parse_number_suffix(valuation.get("Price/Earning Ratio")),
            "dividend_per_share": _parse_number_suffix(valuation.get("Dividend Per Share")),
            "dividend_yield": _parse_pct(valuation.get("Dividend Yield")),
            "shares_outstanding": _parse_number_suffix(valuation.get("Shares Outstanding")),
            "market_cap": _parse_number_suffix(valuation.get("Market Capitalization")),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "afx.kwayisi.org",
        }
        self._quote_cache[ticker] = (now, quote)
        self._accumulate_daily_snapshot(ticker, quote)
        return quote

    # ---------- slow-building free historical cache ----------

    def _accumulate_daily_snapshot(self, ticker: str, quote: dict[str, Any]):
        """Append one row/day to a per-ticker CSV so a real (if initially
        short) daily-close history builds up for free over time, since
        afx.kwayisi.org itself only exposes a live snapshot, not a
        historical series. Safe to call every time get_quote() runs —
        dedupes on calendar date."""
        if quote.get("price") is None:
            return
        cache_file = CACHE_DIR / f"{ticker}_daily.csv"
        today = datetime.now(timezone.utc).date().isoformat()
        row = {
            "date": today, "open": quote["price"], "high": quote.get("day_high") or quote["price"],
            "low": quote.get("day_low") or quote["price"], "close": quote["price"],
            "volume": quote.get("volume") or 0,
        }
        try:
            if cache_file.exists():
                existing = pd.read_csv(cache_file)
                if today in existing["date"].astype(str).values:
                    return
                existing = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
            else:
                existing = pd.DataFrame([row])
            existing.to_csv(cache_file, index=False)
        except Exception as e:
            logger.warning(f"Failed to accumulate NSE daily snapshot for {ticker}: {e}")

    def get_accumulated_history(self, ticker: str) -> pd.DataFrame | None:
        """Daily OHLCV built from accumulated free snapshots (see above).
        Returns None if nothing has accumulated yet — this can take months
        to reach the 200 rows a 200DMA trend check needs; callers should
        treat that as an honest limitation of free NSE data, not a bug."""
        cache_file = CACHE_DIR / f"{ticker.upper()}_daily.csv"
        if not cache_file.exists():
            return None
        try:
            df = pd.read_csv(cache_file, parse_dates=["date"], index_col="date")
            df.sort_index(inplace=True)
            return df
        except Exception:
            return None

    # ---------- historical OHLCV (Apify only — the one real source) ----------

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 200) -> pd.DataFrame:
        """Historical daily OHLCV. Apify (paid) is the only source here that
        provides a real historical series; without it, falls back to
        whatever has accumulated in get_accumulated_history() — which may
        be much shorter than `limit` days, especially early on."""
        ticker = symbol.replace(".NSE", "").replace("/NSE", "").upper()

        if self.apify_token:
            df = self._fetch_via_apify(ticker)
            if df is not None and len(df) > 0:
                return df.tail(limit)

        accumulated = self.get_accumulated_history(ticker)
        if accumulated is not None and len(accumulated) > 0:
            return accumulated.tail(limit)

        return pd.DataFrame(columns=pd.Index(["open", "high", "low", "close", "volume"]))

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
            succeeded = False
            status_resp = None
            for _ in range(30):
                time.sleep(2)
                status_resp = requests.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {self.apify_token}"},
                    timeout=10,
                )
                status = status_resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    succeeded = True
                    break
                elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    return None

            if not succeeded or status_resp is None:
                return None  # still running after ~60s of polling — give up rather than use a stale status

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

            rows: list[dict[str, object]] = []
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

    def latest_price(self, symbol: str) -> float | None:
        """Get latest price — prefers the free live afx.kwayisi.org quote."""
        ticker = symbol.replace(".NSE", "").replace("/NSE", "").upper()
        quote = self.get_quote(ticker)
        if quote and quote.get("price") is not None:
            return quote["price"]
        df = self.get_ohlcv(symbol, timeframe="1d", limit=1)
        if len(df) > 0:
            return float(df.iloc[-1]["close"])
        return None

    def get_nse_tickers(self) -> list[str]:
        """Returns the default NSE watchlist — every security verified
        listed on the exchange as of when DEFAULT_NSE_TICKERS was last
        checked (see its comment). For the true current universe, live,
        use get_market_snapshot()["universe"] instead."""
        return DEFAULT_NSE_TICKERS.copy()
