"""
NSE prices: a Python port of Sokowatch-Web's src/lib/nse-api.ts and src/lib/market.ts.

Same upstream (RapidAPI "nairobi-stock-exchange-nse", GET /stocks?limit=200) and the
same parsing. Two deliberate differences:
  * Quota. The request quota is small and a Sokowatch deployment using the same key
    shares it, so one response is reused for an hour, saved to disk (restarts cost
    nothing) and not re-fetched while the market is shut.
  * No demo prices. These rows feed buy lists, so when there is no real data the
    answer is "no data", never sample numbers.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from stock_analysis import history
from stock_analysis.storage import data_dir

DEFAULT_HOST = "nairobi-stock-exchange-nse.p.rapidapi.com"
EAT = timezone(timedelta(hours=3))          # East Africa Time has no daylight saving
OPEN_MINUTES, CLOSE_MINUTES = 9 * 60, 15 * 60
SETTLE_MINUTES = 30                         # prices are delayed: wait this long after open / close
BACKOFF_SECONDS = 300
STALE_MAX_SECONDS = 7 * 24 * 3600           # older than this is too old to rank anything on
ETF_SECTOR = "Exchange Traded Funds"

_lock = threading.Lock()
_cache: dict = {}                           # {"rows": [...], "fetched_at": unix, "updated": iso}
_backoff_until = 0.0
_last_error = ""


# ---------- parsing (market.ts) ----------
def _number(text) -> float:
    try:
        v = float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and abs(v) != float("inf") else 0.0


def parse_change(text, price: float = 0.0) -> tuple:
    """(absolute change, percent change). Accepts "+0.30 (+1.74%)" or a bare percentage
    ("-1.51"), which is what the live feed sends."""
    s = (text or "").strip()
    both = re.search(r"([+-]?\d*\.?\d+)\s*\(\s*([+-]?\d*\.?\d+)\s*%\s*\)", s)
    if both:
        return float(both.group(1)), float(both.group(2))
    bare = re.match(r"^([+-]?\d*\.?\d+)\s*%?$", s)
    if not bare:
        return 0.0, 0.0
    pct = float(bare.group(1))
    if not price or price <= 0 or pct <= -100:
        return 0.0, pct
    return round(price - price / (1 + pct / 100), 2), pct


def clean_name(name: str) -> str:
    """Strips the par-value suffix: "Safaricom Ltd Ord 0.05" becomes "Safaricom Ltd"."""
    s = re.sub(r"\s+(?:Ord\.?\s*)?\d+(?:\.\d+)?\s*$", "", name or "", flags=re.I)
    s = re.sub(r"\s+Ord\.?\s*$", "", s, flags=re.I).strip()
    return s or (name or "").strip()


def transform(raw: dict) -> dict:
    price = _number(raw.get("price"))
    change, pct = parse_change(raw.get("change"), price)
    sector = (raw.get("sector") or "").strip() or "Other"
    symbol = str(raw.get("ticker", "")).upper()
    return {"symbol": symbol, "name": clean_name(raw.get("name") or symbol), "sector": sector,
            "kind": "etf" if sector == ETF_SECTOR else "stock", "price": price, "change": change,
            "change_pct": pct, "volume": _number(raw.get("volume")), "currency": "KES"}


# ---------- trading hours ----------
def market_status(now: datetime | None = None) -> dict:
    """Mon-Fri 09:00-15:00 EAT. Public holidays are not modelled."""
    local = (now or datetime.now(timezone.utc)).astimezone(EAT)
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return {"is_open": False, "message": "NSE is closed for the weekend"}
    if OPEN_MINUTES <= minutes < CLOSE_MINUTES:
        return {"is_open": True, "message": "NSE is open (closes 15:00 EAT)"}
    return {"is_open": False, "message": "NSE is closed" + (" (opens 09:00 EAT)" if minutes < OPEN_MINUTES else "")}


def last_session_end(now: datetime) -> datetime:
    """The most recent weekday close, plus the settle margin."""
    local = now.astimezone(EAT)
    end = local.replace(hour=15, minute=SETTLE_MINUTES, second=0, microsecond=0)
    while end > local or end.weekday() >= 5:
        end -= timedelta(days=1)
    return end


def snapshot_date(now: datetime) -> str | None:
    """The trading day a price fetched at `now` belongs to, or None if it should not be
    recorded (weekend, or before today's session has produced prices)."""
    local = now.astimezone(EAT)
    if local.weekday() >= 5 or local.hour * 60 + local.minute < OPEN_MINUTES + SETTLE_MINUTES:
        return None
    return local.date().isoformat()


def needs_refresh(fetched_at: float, now: float, ttl: int) -> bool:
    if now - fetched_at < ttl:
        return False
    moment = datetime.fromtimestamp(now, timezone.utc)
    local = moment.astimezone(EAT)
    in_session = (local.weekday() < 5 and
                  OPEN_MINUTES <= local.hour * 60 + local.minute < CLOSE_MINUTES + SETTLE_MINUTES)
    # Shut market and we already hold the closing prices: another request would buy nothing.
    return in_session or fetched_at < last_session_end(moment).timestamp()


# ---------- fetching ----------
def _ttl() -> int:
    try:
        return max(300, int(os.environ.get("NSE_REVALIDATE_SECONDS", "") or 3600))
    except ValueError:
        return 3600


def _cache_file():
    return data_dir() / "nse_last.json"


def _load_disk() -> None:
    try:
        saved = json.loads(_cache_file().read_text(encoding="utf-8"))
        if isinstance(saved.get("rows"), list) and saved.get("fetched_at"):
            _cache.update(saved)
    except (OSError, ValueError, AttributeError):
        pass


def _fetch_upstream(key: str) -> dict:
    host = os.environ.get("RAPIDAPI_HOST") or DEFAULT_HOST
    r = requests.get(f"https://{host}/stocks", params={"limit": 200}, timeout=10,
                     headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": host})
    if r.status_code != 200:
        raise RuntimeError(f"NSE API error: HTTP {r.status_code}")
    body = r.json()
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("NSE API returned an unexpected payload")
    return {"rows": rows, "fetched_at": time.time(),
            "updated": (body.get("meta") or {}).get("lastUpdated") or datetime.now(timezone.utc).isoformat()}


def _save(fresh: dict) -> None:
    """Keep the response and today's closes. A read-only disk must not break the page."""
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(fresh), encoding="utf-8")
        day = snapshot_date(datetime.fromtimestamp(fresh["fetched_at"], timezone.utc))
        if day:
            stocks = [transform(r) for r in fresh["rows"]]
            history.record(day, stocks)
            history.record_tick(int(fresh["fetched_at"]), stocks)
    except Exception as e:
        print(f"[stocks] could not save NSE data: {e}")


def get_quotes() -> dict:
    """{"stocks": [...], "source": "live" | "stale" | "unavailable", "updated", "note"}."""
    global _backoff_until, _last_error
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    with _lock:
        if not _cache:
            _load_disk()
        now = time.time()
        wanted = not _cache or needs_refresh(_cache["fetched_at"], now, _ttl())
        failed = False
        if wanted and key and now >= _backoff_until:
            try:
                fresh = _fetch_upstream(key)
                _cache.clear()
                _cache.update(fresh)
                _last_error, wanted = "", False
                _save(fresh)
            except Exception as e:
                _last_error = str(e)[:200]
                _backoff_until = now + BACKOFF_SECONDS
                failed = True
        if wanted and not key:
            note = "RAPIDAPI_KEY is not set. Add it to .env (same key as Sokowatch-Web) and restart the dashboard."
        elif wanted or failed:
            note = _last_error or "NSE feed unavailable"
        else:
            note = ""
        if not _cache or now - _cache["fetched_at"] > STALE_MAX_SECONDS:
            return {"stocks": [], "source": "unavailable", "updated": None, "note": note}
        stocks = [s for s in map(transform, _cache["rows"]) if s["symbol"] and s["price"] > 0]
        return {"stocks": stocks, "source": "stale" if wanted else "live",
                "updated": _cache["updated"], "note": note}
