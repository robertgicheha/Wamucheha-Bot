"""
NSE Kenya data scraper.

The Nairobi Securities Exchange does NOT offer a public trading API. There is no
ccxt support, no OANDA equivalent, no broker with open algo-access for NSE.

Data sources, in priority order:
1. RapidAPI "Nairobi Stock Exchange (NSE)" (paid, requires NSE_RAPIDAPI_KEY) —
   the structured source of record for the exchange-wide price feed.
   GET /stocks returns EVERY listed security in a single request
   (ticker, name, ISIN, volume, price, day change %, sector), which is what
   refresh_market_snapshot() persists once per trading day. See the quota
   note on refresh_market_snapshot() before adding any other call site.
2. afx.kwayisi.org (free, no key, no signup) — a live NSE quote aggregator.
   Verified working against real requests: the exchange-wide page
   (afx.kwayisi.org/nse/) exposes the full listed-companies table plus
   ready-made Top Gainers / Bottom Losers tables in ONE request, and each
   ticker's page (afx.kwayisi.org/nse/<ticker>/) exposes price, day
   low/high, volume, and fundamentals (EPS, P/E, dividend yield, market
   cap) — this is what get_quote() uses, and it's also what fills the
   fundamentals gap noted in long_term/fundamentals.py, because RapidAPI's
   /stocks endpoint returns no fundamentals at all. It also serves as the
   free between-days fallback for the exchange-wide snapshot.
   It does NOT expose a historical daily-close time series though (only a
   live snapshot + 1WK/4WK/3MO % performance), so it cannot alone support a
   200-day-moving-average trend check — see _accumulate_daily_snapshot().
3. Local CSV cache, including a slow-building one built by appending one
   snapshot row per calendar day (see _accumulate_daily_snapshot and
   record_daily_snapshot) so trend_context-style analysis and the forecast
   module become possible for free after enough days have accumulated,
   without ever promising it's available on day one.

CRITICAL LIMITATION: NSE is alert/analysis only — NOT automated execution.
The long_term/screener.py uses this for trend context on Kenyan stocks, but
any actual NSE trade would need to go through your broker (Genghis Capital,
AIB-AXYS, Faida) manually or via their proprietary FIX/API if they offer one.
"""
import os
import re
import time
import json
import logging
from typing import TYPE_CHECKING, Any

import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta, date as _date

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

# ---------- RapidAPI: "Nairobi Stock Exchange (NSE)" ----------

# Primary structured price feed for the whole exchange. The Basic plan allows
# only 4 requests/hour and 250/month, so the quota is a hard design constraint
# here: see refresh_market_snapshot() for how that is enforced.
RAPIDAPI_HOST = os.environ.get(
    "NSE_RAPIDAPI_HOST", "nairobi-stock-exchange-nse.p.rapidapi.com"
).strip()
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"
SNAPSHOT_FILE = CACHE_DIR / "rapidapi_snapshot.json"

# A persisted snapshot older than this is treated as stale (covers weekends
# and public holidays, when no new snapshot is written).
SNAPSHOT_MAX_AGE = timedelta(hours=36)


def _nse_today() -> _date:
    """Today's date in East Africa Time.

    NSE trades 09:00-15:00 EAT (06:00-12:00 UTC), so a UTC calendar date is
    the *previous* day for any snapshot taken before 03:00 EAT. Using the
    Nairobi date keeps one trading day mapped to one row, which is what the
    per-ticker CSVs and the forecast windows depend on.
    """
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _nse_time() -> datetime:
    """Current time in East Africa Time (UTC+3, no DST)."""
    return datetime.now(timezone.utc) + timedelta(hours=3)


def _rapidapi_headers(key: str) -> dict[str, str]:
    return {"x-rapidapi-key": key, "x-rapidapi-host": RAPIDAPI_HOST}


def _to_float(value: Any) -> float | None:
    """RapidAPI returns every number as a string ("36.50", ".45", "0.00")."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in ("null", "none", "n/a", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None

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


_PERF_KEYS = {"1WK": "return_1w", "4WK": "return_4w", "3MO": "return_3m",
              "6MO": "return_6m", "1YR": "return_1y", "YTD": "return_ytd"}


def _parse_performance(soup: Any) -> dict[str, float | None]:
    """<div data-perf> holds two small tables: 1WK/4WK/3MO and 6MO/1YR/YTD."""
    out: dict[str, float | None] = {v: None for v in _PERF_KEYS.values()}
    block = soup.find("div", attrs={"data-perf": True})
    if not block:
        return out
    for table in block.find_all("table"):
        heads = [th.get_text(strip=True) for th in table.find_all("th")]
        cells = [td.get_text(strip=True) for td in table.find_all("td")]
        for head, cell in zip(heads, cells):
            if head in _PERF_KEYS:
                out[_PERF_KEYS[head]] = _parse_pct(cell)
    return out


def _kv_table(table: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            out[tds[0].get_text(strip=True)] = tds[1].get_text(strip=True)
    return out


class NSEFeed:
    def __init__(self, rapidapi_key: str | None = None):
        self.rapidapi_key = rapidapi_key or os.environ.get("NSE_RAPIDAPI_KEY")
        self._snapshot_cache = None
        self._snapshot_cache_ts = 0
        self._snapshot_cache_ttl = 900  # 15 min — index page is cheap, refresh often
        self._quote_cache = {}
        self._quote_cache_ttl = 900

    # ---------- RapidAPI: the one quota-burning call site ----------

    @property
    def rapidapi_configured(self) -> bool:
        return bool(self.rapidapi_key)

    def refresh_market_snapshot(self, force_refresh: bool = False) -> dict[str, Any] | None:
        """Fetch the whole exchange from RapidAPI GET /stocks, persist it, and
        append one row per ticker to the daily history CSVs.

        THIS IS THE ONLY FUNCTION THAT CALLS RAPIDAPI. Quota is 4 requests/hour
        and 250/month on the Basic plan, so at one call per trading day this
        costs ~22 calls/month. Nothing else in this codebase — the hourly
        dashboard refresh included — may call it directly; every other reader
        goes through get_market_snapshot(), which prefers the persisted file.
        Call it from the 16:00 EAT job, not from a per-request code path.
        """
        if not self.rapidapi_key:
            logger.warning("NSE_RAPIDAPI_KEY not set — cannot refresh RapidAPI snapshot")
            return None

        persisted = self.load_persisted_snapshot()
        if not force_refresh and persisted and not persisted.get("stale"):
            logger.info("RapidAPI NSE snapshot already persisted and fresh — skipping call")
            return persisted

        try:
            resp = requests.get(
                f"{RAPIDAPI_BASE}/stocks",
                headers=_rapidapi_headers(self.rapidapi_key),
                timeout=25,
            )
        except Exception as e:
            logger.warning(f"NSE RapidAPI /stocks fetch failed: {e}")
            return persisted

        if resp.status_code == 429:
            logger.warning("NSE RapidAPI rate limited (429) — keeping previous snapshot")
            return persisted
        if resp.status_code != 200:
            logger.warning(f"NSE RapidAPI /stocks returned HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
            return persisted

        try:
            payload = resp.json()
        except ValueError as e:
            logger.warning(f"NSE RapidAPI /stocks returned non-JSON body: {e}")
            return persisted

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            logger.warning(f"NSE RapidAPI /stocks unexpected shape: {type(payload).__name__}")
            return persisted

        stocks = []
        for row in payload["data"]:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            price = _to_float(row.get("price"))
            volume = _to_int(row.get("volume"))
            change_pct = _to_float(row.get("change"))
            stocks.append({
                "ticker": ticker,
                "name": (row.get("name") or ticker).strip(),
                "isin": (row.get("isin") or None),
                "sector": (row.get("sector") or None),
                "price": price,
                # "change" is the day move in PERCENT POINTS vs previous close.
                "change_pct": change_pct,
                "volume": volume,
                # A listed name that traded nothing today is stale, not delisted
                # — keep it, the user asked for all stocks, but flag it so the
                # dashboard can grey it out instead of implying a live price.
                "stale": bool(volume == 0),
            })

        if not stocks:
            logger.warning("NSE RapidAPI /stocks returned zero usable rows — keeping previous snapshot")
            return persisted

        snapshot = {
            "stocks": stocks,
            "meta": payload.get("meta") or {},
            "api_timestamp": payload.get("timestamp"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "trading_date": _nse_today().isoformat(),
            "source": "rapidapi",
        }
        self._persist_snapshot(snapshot)
        self.record_daily_snapshot(snapshot)
        logger.info(f"RapidAPI NSE snapshot: {len(stocks)} securities persisted")
        return snapshot

    def load_persisted_snapshot(self) -> dict[str, Any] | None:
        """Read the last RapidAPI snapshot from disk. Safe for any process —
        the scheduler writes it, the dashboard and the bot's main loop read it,
        and none of those readers spend quota."""
        if not SNAPSHOT_FILE.exists():
            return None
        try:
            snapshot = json.loads(SNAPSHOT_FILE.read_text())
        except (ValueError, OSError) as e:
            logger.warning(f"Could not read persisted NSE snapshot: {e}")
            return None
        if not isinstance(snapshot, dict) or not snapshot.get("stocks"):
            return None
        snapshot["stale"] = self._snapshot_is_stale(snapshot)
        return snapshot

    @staticmethod
    def _snapshot_is_stale(snapshot: dict[str, Any]) -> bool:
        fetched_at = snapshot.get("fetched_at")
        if not fetched_at:
            return True
        try:
            fetched = datetime.fromisoformat(fetched_at)
        except (TypeError, ValueError):
            return True
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched) > SNAPSHOT_MAX_AGE

    def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Atomic write — the dashboard may read this file at any moment."""
        tmp = SNAPSHOT_FILE.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(snapshot, indent=2, default=str))
            tmp.replace(SNAPSHOT_FILE)
        except OSError as e:
            logger.warning(f"Failed to persist NSE snapshot: {e}")

    # ---------- free live snapshot (afx.kwayisi.org) ----------

    def get_market_snapshot(self, force_refresh: bool = False) -> dict[str, Any] | None:
        """One exchange-wide snapshot in a single request, regardless of source.

        Prefers the persisted RapidAPI /stocks file when one exists (it carries
        every listed security plus ISIN and sector, which the AFX scrape lacks),
        and falls back to the free afx.kwayisi.org scrape otherwise. Both paths
        return the SAME shape so every existing consumer keeps working:
            {"gainers": [...], "losers": [...], "universe": [...],
             "fetched_at": ..., "source": ...}
        Returns None if no source is reachable (caller should skip).
        """
        persisted = self.load_persisted_snapshot()
        if persisted is not None:
            return self._snapshot_as_movers(persisted)

        return self._get_afx_snapshot(force_refresh=force_refresh)

    @staticmethod
    def _snapshot_as_movers(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Map a RapidAPI /stocks snapshot onto the existing
        gainers/losers/universe contract. Gainers and losers are derived by
        sorting on change_pct, since the RapidAPI endpoint has no per-sector
        "top movers" tables."""
        universe = []
        for stock in snapshot.get("stocks", []):
            change_pct = stock.get("change_pct")
            universe.append({
                "ticker": stock["ticker"],
                "name": stock.get("name"),
                "isin": stock.get("isin"),
                "sector": stock.get("sector"),
                "volume": stock.get("volume"),
                "price": stock.get("price"),
                "change_pct": change_pct,
                "direction": ("up" if (change_pct or 0) > 0
                              else "down" if (change_pct or 0) < 0 else "flat"),
                "stale": bool(stock.get("stale")),
            })

        ranked = [u for u in universe if u["change_pct"] is not None]
        gainers = sorted(ranked, key=lambda u: u["change_pct"], reverse=True)
        losers = sorted(ranked, key=lambda u: u["change_pct"])

        out = dict(snapshot)
        out["universe"] = universe
        out["gainers"] = gainers
        out["losers"] = losers
        out["sectors"] = NSEFeed._sector_breakdown(universe)
        return out

    @staticmethod
    def _sector_breakdown(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per-sector counts and total traded value, for the dashboard."""
        by_sector: dict[str, dict[str, Any]] = {}
        for row in universe:
            sector = row.get("sector") or "Unclassified"
            entry = by_sector.setdefault(sector, {"sector": sector, "count": 0,
                                                   "traded_value": 0.0})
            entry["count"] += 1
            price, volume = row.get("price"), row.get("volume")
            if price and volume:
                entry["traded_value"] += price * volume
        return sorted(by_sector.values(), key=lambda e: e["traded_value"], reverse=True)

    def _get_afx_snapshot(self, force_refresh: bool = False) -> dict[str, Any] | None:
        """The free afx.kwayisi.org fallback: the exchange-wide page exposes the
        full listed-companies table plus ready-made Top Gainers / Bottom Losers
        tables in ONE request. Used when no RapidAPI snapshot has been persisted
        yet, and between trading days when prices have moved on."""
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
            "sectors": self._sector_breakdown(parse_universe(tables[3])),
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
            # Price returns from the page's performance block (1WK..YTD) —
            # real multi-month momentum without needing our own history.
            **_parse_performance(soup),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "afx.kwayisi.org",
        }
        self._quote_cache[ticker] = (now, quote)
        self._accumulate_daily_snapshot(ticker, quote)
        return quote

    # ---------- slow-building free historical cache ----------

    def record_daily_snapshot(self, snapshot: dict[str, Any]) -> int:
        """Append one row per ticker to the daily history CSVs, from a RapidAPI
        /stocks snapshot.

        This is what actually builds the chart and forecast history: the
        RapidAPI Basic plan serves only the live /stocks list (history
        endpoints are Pro-only), so the time series is accumulated locally,
        one close per trading day, for every security on the exchange.
        /stocks carries no intraday high/low, so open=high=low=close=price.
        Returns the number of tickers written.
        """
        written = 0
        trading_date = snapshot.get("trading_date") or _nse_today().isoformat()
        for stock in snapshot.get("stocks", []):
            price = stock.get("price")
            if not price:
                continue
            if self._append_history_row(
                stock["ticker"], trading_date,
                open_=price, high=price, low=price, close=price,
                volume=stock.get("volume") or 0,
            ):
                written += 1
        logger.info(f"Recorded NSE daily close for {written}/{len(snapshot.get('stocks', []))} "
                    f"securities ({trading_date})")
        return written

    def _append_history_row(self, ticker: str, day: str, open_: float, high: float,
                            low: float, close: float, volume: float) -> bool:
        """Append or replace one trading day in a ticker's CSV. Returns True if
        the file was written. Dedupes on date so re-running is safe."""
        cache_file = CACHE_DIR / f"{ticker.upper()}_daily.csv"
        row = {"date": day, "open": open_, "high": high, "low": low,
               "close": close, "volume": int(volume or 0)}
        try:
            if cache_file.exists():
                existing = pd.read_csv(cache_file)
                if day in existing["date"].astype(str).values:
                    return False
                existing = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
            else:
                existing = pd.DataFrame([row])
            existing.to_csv(cache_file, index=False)
            return True
        except Exception as e:
            logger.warning(f"Failed to accumulate NSE daily snapshot for {ticker}: {e}")
            return False

    def _accumulate_daily_snapshot(self, ticker: str, quote: dict[str, Any]):
        """Append one row/day to a per-ticker CSV so a real (if initially
        short) daily-close history builds up for free over time, since
        neither afx.kwayisi.org nor RapidAPI's /stocks exposes a historical
        series. Safe to call every time get_quote() runs — dedupes on the
        East Africa trading date, not the UTC one, so a 02:00 UTC run and a
        16:00 EAT run land on the same trading day rather than two."""
        if quote.get("price") is None:
            return
        price = quote["price"]
        self._append_history_row(
            ticker, _nse_today().isoformat(),
            open_=price,
            high=quote.get("day_high") or price,
            low=quote.get("day_low") or price,
            close=price,
            volume=quote.get("volume") or 0,
        )

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

    # ---------- historical OHLCV (locally accumulated) ----------

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 200) -> pd.DataFrame:
        """Historical daily OHLCV, served from the locally accumulated CSV.

        There is no backfill: the RapidAPI history endpoints are Pro-only, so
        the series starts at the first scheduled 16:00 EAT snapshot and grows
        one row per trading day. Callers must handle a short series — the
        screener's 200DMA check needs 200 rows, i.e. roughly 10 months, and the
        trend/forecast code degrades honestly below that."""
        ticker = symbol.replace(".NSE", "").replace("/NSE", "").upper()

        accumulated = self.get_accumulated_history(ticker)
        if accumulated is not None and len(accumulated) > 0:
            return accumulated.tail(limit)

        return pd.DataFrame(columns=pd.Index(["open", "high", "low", "close", "volume"]))

    def latest_price(self, symbol: str) -> float | None:
        """Get latest price — the persisted RapidAPI snapshot first (it is
        fresher and costs no quota), then the free AFX quote, then the last
        accumulated close."""
        ticker = symbol.replace(".NSE", "").replace("/NSE", "").upper()
        for stock in (self.load_persisted_snapshot() or {}).get("stocks", []):
            if stock["ticker"] == ticker and stock.get("price"):
                return float(stock["price"])
        quote = self.get_quote(ticker)
        if quote and quote.get("price") is not None:
            return quote["price"]
        df = self.get_ohlcv(symbol, timeframe="1d", limit=1)
        if len(df) > 0:
            return float(df.iloc[-1]["close"])
        return None

    def get_nse_tickers(self) -> list[str]:
        """The full listed universe.

        Prefers the persisted RapidAPI snapshot, which is the true current
        list and picks up listings/delistings automatically. Falls back to
        DEFAULT_NSE_TICKERS — a point-in-time snapshot of every security
        verified on the exchange when that constant was last checked, which
        is how fundamentals.py decides whether a bare ticker is Kenyan."""
        snapshot = self.load_persisted_snapshot()
        if snapshot:
            return [stock["ticker"] for stock in snapshot["stocks"]]
        return DEFAULT_NSE_TICKERS.copy()
