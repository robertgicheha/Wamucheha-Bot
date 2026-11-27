"""
Company and fund figures, financial statements and news.

US: Yahoo Finance. One request per company, so figures load in the background and are
cached on disk for a day.
NSE: the RapidAPI feed carries none of this. Only what you put in
config/nse_fundamentals.csv is shown; anything else stays empty.
"""
import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree

import requests

from stock_analysis.storage import ROOT, data_dir

TTL = 24 * 3600
FAIL_TTL = 600
NSE_CSV = ROOT / "config" / "nse_fundamentals.csv"

# our name -> Yahoo "info" key
_YAHOO = {
    "market_cap": "marketCap", "pe": "trailingPE", "forward_pe": "forwardPE", "eps": "trailingEps",
    "beta": "beta", "employees": "fullTimeEmployees", "revenue": "totalRevenue", "gross_profit": "grossProfits",
    "operating_cash_flow": "operatingCashflow", "free_cash_flow": "freeCashflow", "ebitda": "ebitda",
    "net_income": "netIncomeToCommon", "cash": "totalCash", "debt": "totalDebt", "debt_to_equity": "debtToEquity",
    "current_ratio": "currentRatio", "quick_ratio": "quickRatio", "ps": "priceToSalesTrailing12Months",
    "pb": "priceToBook", "enterprise_value": "enterpriseValue", "ev_ebitda": "enterpriseToEbitda",
    "float_shares": "floatShares", "shares_outstanding": "sharesOutstanding", "aum": "totalAssets",
    "expense_ratio_pct": "netExpenseRatio", "nav": "navPrice",
}
_PERCENT = {"eps_growth_pct": "earningsGrowth", "revenue_growth_pct": "revenueGrowth",
            "gross_margin_pct": "grossMargins", "operating_margin_pct": "operatingMargins",
            "net_margin_pct": "profitMargins", "roa_pct": "returnOnAssets", "roe_pct": "returnOnEquity",
            "payout_ratio_pct": "payoutRatio"}
_TEXT = {"sector": "sector", "industry": "industry", "exchange": "fullExchangeName",
         "category": "category", "fund_family": "fundFamily", "website": "website",
         "about": "longBusinessSummary", "long_name": "longName"}
_RATINGS = {"strong_buy": "Strong buy", "buy": "Buy", "hold": "Neutral", "underperform": "Sell",
            "sell": "Sell", "strong_sell": "Strong sell"}


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _per_employee(f: dict) -> None:
    staff = f.get("employees")
    for total, name in (("revenue", "revenue_per_employee"), ("net_income", "profit_per_employee")):
        if staff and f.get(total) is not None:
            f[name] = f[total] / staff


def from_yahoo(info: dict) -> dict:
    out: dict = {}
    for name, key in _YAHOO.items():
        value = _finite(info.get(key))
        if value is not None:
            out[name] = value
    for name, key in _PERCENT.items():
        value = _finite(info.get(key))
        if value is not None:
            out[name] = round(value * 100, 2)
    for name, key in _TEXT.items():
        if info.get(key):
            out[name] = str(info[key])
    if "beta" not in out and _finite(info.get("beta3Year")) is not None:      # funds report this one
        out["beta"] = info["beta3Year"]
    rating = _RATINGS.get(str(info.get("recommendationKey", "")).lower())
    if rating:
        out["analyst_rating"] = rating
    _per_employee(out)
    return out


# ---------- US, loaded in the background ----------
_lock = threading.Lock()
_cache: dict = {}          # symbol -> {"at": unix, "data": dict}
_loaded = False
_worker = None


def _file():
    return data_dir() / "us_fundamentals.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        _cache.update(json.loads(_file().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass


def _fetch_info(symbol: str) -> dict:
    import yfinance as yf
    return yf.Ticker(symbol.replace(".", "-")).info or {}


def _store(symbol: str) -> None:
    try:
        data = from_yahoo(_fetch_info(symbol))
    except Exception as e:
        print(f"[stocks] fundamentals for {symbol} failed: {e}")
        data = {}
    with _lock:
        if data:
            _cache[symbol] = {"at": time.time(), "data": data}
        else:   # keep older figures if we have them, and retry soon rather than in a day
            old = _cache.get(symbol, {}).get("data", {})
            _cache[symbol] = {"at": time.time() - TTL + FAIL_TTL, "data": old}


def _fill(symbols: list) -> None:
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_store, symbols))
    try:
        _file().parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            text = json.dumps(_cache)
        _file().write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"[stocks] could not save fundamentals: {e}")


def us_many(symbols: list) -> tuple:
    """(figures by symbol, number still waiting). Returns at once; missing ones load behind it."""
    global _worker
    with _lock:
        _load()
        now = time.time()
        due = [s for s in symbols if s not in _cache or now - _cache[s]["at"] >= TTL]
        if due and not (_worker and _worker.is_alive()):
            _worker = threading.Thread(target=_fill, args=(due,), daemon=True)
            _worker.start()
        return ({s: _cache[s]["data"] for s in symbols if s in _cache},
                sum(s not in _cache for s in symbols))


def us_one(symbol: str) -> dict:
    """Figures for one security, fetched now if needed (the detail page waits for them)."""
    with _lock:
        _load()
        hit = _cache.get(symbol)
    if not hit or time.time() - hit["at"] >= TTL:
        _store(symbol)
    with _lock:
        return _cache.get(symbol, {}).get("data", {})


# ---------- NSE, from a file you fill in ----------
_NSE_TEXT = {"analyst_rating", "sector", "industry", "about", "website", "long_name", "book_closure"}


def nse_table() -> dict:
    """{symbol: figures} from config/nse_fundamentals.csv. Blank or unreadable cells are skipped."""
    try:
        with open(NSE_CSV, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError):
        return {}
    out = {}
    for raw in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if isinstance(v, str)}
        symbol = r.pop("symbol", "").upper()
        if not symbol or symbol.startswith("#"):
            continue
        figures = {}
        for key, value in r.items():
            if not value:
                continue
            if key in _NSE_TEXT:
                figures[key] = value
                continue
            try:
                number = _finite(float(value.replace(",", "").rstrip("%")))
            except ValueError:
                continue
            if number is not None:
                figures[key] = number
        _per_employee(figures)
        out[symbol] = figures
    return out


# ---------- statements and fund holdings ----------
_STATEMENTS = (("income", "Income statement", "income_stmt"), ("balance", "Balance sheet", "balance_sheet"),
               ("cashflow", "Cash flow", "cashflow"))
_LINES = {
    "income": ["Total Revenue", "Cost Of Revenue", "Gross Profit", "Operating Expense", "Operating Income",
               "Pretax Income", "Tax Provision", "Net Income", "EBITDA", "Diluted EPS"],
    "balance": ["Total Assets", "Current Assets", "Cash And Cash Equivalents", "Inventory",
                "Total Liabilities Net Minority Interest", "Current Liabilities", "Total Debt", "Net Debt",
                "Stockholders Equity", "Ordinary Shares Number"],
    "cashflow": ["Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow", "Capital Expenditure",
                 "Free Cash Flow", "Cash Dividends Paid", "Repurchase Of Capital Stock", "End Cash Position"],
}
_extra_cache: dict = {}    # (kind, symbol) -> (unix, value)


def _cached(key, ttl: int, build):
    hit = _extra_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = build()
    _extra_cache[key] = (time.time(), value)
    return value


def statements(symbol: str, quarterly: bool = False) -> list:
    def build():
        import yfinance as yf
        ticker = yf.Ticker(symbol.replace(".", "-"))
        out = []
        for key, title, attr in _STATEMENTS:
            df = getattr(ticker, ("quarterly_" if quarterly else "") + attr)
            if df is None or df.empty:
                continue
            columns = list(df.columns)[:5][::-1]          # oldest first
            lines = [{"name": line, "values": [_finite(float(v)) for v in df.loc[line, columns]]}
                     for line in _LINES[key] if line in df.index]
            # Yahoo often pads the oldest period with nothing at all: drop empty columns.
            used = [i for i in range(len(columns)) if any(line["values"][i] is not None for line in lines)]
            columns = [columns[i] for i in used]
            lines = [{**line, "values": [line["values"][i] for i in used]} for line in lines]
            out.append({"key": key, "title": title, "periods": [c.date().isoformat() for c in columns],
                        "lines": lines})
        return out
    return _cached(("statements", symbol, quarterly), 6 * 3600, build)


def fund_profile(symbol: str) -> dict:
    def build():
        import yfinance as yf
        data = yf.Ticker(symbol.replace(".", "-")).funds_data
        holdings = [{"symbol": str(i), "name": str(r["Name"]), "weight_pct": round(float(r["Holding Percent"]) * 100, 2)}
                    for i, r in data.top_holdings.iterrows()]
        sectors = sorted(({"name": k.replace("_", " ").title(), "weight_pct": round(float(v) * 100, 2)}
                          for k, v in data.sector_weightings.items() if v), key=lambda x: -x["weight_pct"])
        return {"holdings": holdings, "sectors": sectors}
    return _cached(("fund", symbol), TTL, build)


# ---------- news ----------
def _safe_url(url) -> str:
    url = str(url or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def us_news(symbol: str) -> list:
    def build():
        import yfinance as yf
        out = []
        for item in yf.Ticker(symbol.replace(".", "-")).news or []:
            c = item.get("content") or item
            url = _safe_url((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                            or c.get("link"))
            if c.get("title") and url:
                out.append({"title": c["title"], "summary": (c.get("summary") or "")[:300], "url": url,
                            "source": (c.get("provider") or {}).get("displayName") or "Yahoo Finance",
                            "published": c.get("pubDate") or ""})
        return out
    return _cached(("news", symbol), 600, build)


def parse_rss(text: str, fallback_source: str) -> list:
    """RSS 2.0 items. Feeds declaring a DTD are refused: headlines never need entities."""
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return []
    try:
        channel = ElementTree.fromstring(text.strip()).find("channel")
    except ElementTree.ParseError:
        return []
    if channel is None:
        return []
    source = (channel.findtext("title") or fallback_source).strip()
    out = []
    for item in channel.findall("item"):
        title, url = unescape((item.findtext("title") or "").strip()), _safe_url(item.findtext("link"))
        if not title or not url:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            published = ""
        out.append({"title": title, "url": url, "summary": "", "source": source, "published": published})
    return out


def nse_news(symbol: str, name: str) -> dict:
    """Headlines from NEWS_FEED_URLS (Sokowatch-Web's setting), matched to the company."""
    feeds = [u.strip() for u in os.environ.get("NEWS_FEED_URLS", "").split(",") if _safe_url(u)]
    if not feeds:
        return {"items": [], "note": "No news source is set. Add RSS feed addresses to NEWS_FEED_URLS in .env."}

    def build():
        found = []
        for url in feeds[:8]:
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "stocks-module"})
                if r.status_code == 200:
                    found += parse_rss(r.text, url)
            except requests.RequestException:
                continue
        return sorted(found, key=lambda n: n["published"], reverse=True)

    everything = _cached(("rss", tuple(feeds)), 900, build)
    first = name.split()[0].lower() if name else ""
    mine = [n for n in everything
            if symbol.lower() in n["title"].lower().split() or (len(first) > 3 and first in n["title"].lower())]
    if mine:
        return {"items": mine[:20], "note": ""}
    return {"items": everything[:15],
            "note": f"No headline mentions {symbol}; showing the latest from your feeds." if everything
            else "Your news feeds returned nothing."}
