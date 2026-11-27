"""
Stocks & ETFs research API for the admin console (mounted under /api/admin/stocks).

GET only and behind the same X-Dashboard-Key as the rest of the console, so it also
works on a read-only dashboard. The tab's script, style sheet and company logos are
served without a key (a <script> or <img> tag cannot send one); they hold no data.
"""
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from dashboard.admin import require_key
from stock_analysis import service

router = APIRouter(prefix="/api/admin/stocks", dependencies=[Depends(require_key)])
assets = APIRouter(prefix="/stocks-assets")

ASSET_DIR = Path(__file__).parent / "stocks_ui"
_FILES = {"stocks.js": "text/javascript", "stocks.css": "text/css"}
_LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg"}


@assets.get("/logos/{symbol}")
def logo(symbol: str):
    if re.fullmatch(r"[A-Z0-9]{1,10}", symbol):
        for suffix, kind in _LOGO_TYPES.items():
            path = ASSET_DIR / "logos" / (symbol + suffix)
            if path.is_file():
                # nosniff + CSP: an SVG opened directly must not be able to run script here.
                return FileResponse(path, media_type=kind, headers={
                    "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
    raise HTTPException(404, "no logo")


@assets.get("/{name}")
def asset(name: str):
    if name not in _FILES:
        raise HTTPException(404, "not found")
    return FileResponse(ASSET_DIR / name, media_type=_FILES[name], headers={"Cache-Control": "no-cache"})


def _run(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.get("/market")
def market(market: str = "nse", risk: str = "balanced", live: bool = False):
    return _run(service.market_view, market, risk, live)


@router.get("/detail")
def detail(symbol: str, market: str = "nse", risk: str = "balanced", live: bool = False):
    return _run(service.detail, market, symbol, risk, live)


@router.get("/intraday")
def intraday(symbol: str, market: str = "nse", range: str = "1d"):
    return _run(service.intraday, market, symbol, range)


@router.get("/financials")
def financials(symbol: str, market: str = "nse", quarterly: bool = False):
    return _run(service.financials, market, symbol, quarterly)


@router.get("/news")
def news(symbol: str, market: str = "nse"):
    return _run(service.news, market, symbol)
