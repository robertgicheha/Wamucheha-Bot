"""Builds the market view and per-security detail for the admin console and the CLI."""
from datetime import datetime, timezone

import yaml

from stock_analysis import analytics, fundamentals, history, metrics, nse, screener, screens, us
from stock_analysis.storage import ROOT

CONFIG_PATH = ROOT / "config" / "stocks.yaml"
MARKETS = ("nse", "us")
ANALYSIS_BARS = 1260      # scores and the outlook look at the last five years
INTRADAY_RANGES = ("1d", "5d")


def load_config() -> dict:
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"[stocks] could not read {CONFIG_PATH.name}: {e}")
        return {}


def universe(config: dict) -> dict:
    """{ticker: (name, kind)} for the configured US list."""
    cfg = config.get("us") or {}
    return {str(sym).upper(): (str(name), kind)
            for key, kind in (("stocks", "stock"), ("etfs", "etf"))
            for sym, name in (cfg.get(key) or {}).items()}


def nse_dividend(entry, price: float) -> dict | None:
    """A user-entered dividend figure; ignored unless it is a positive number."""
    try:
        per_share = float(entry["dividend_per_share"])
    except (TypeError, KeyError, ValueError):
        return None
    if per_share <= 0 or price <= 0:
        return None
    return {"annual_per_share": per_share, "yield_pct": round(per_share / price * 100, 2),
            "book_closure": str(entry.get("book_closure") or ""), "note": str(entry.get("note") or ""),
            "source": "entered in config/stocks.yaml"}


def _us_row(symbol: str, name: str, kind: str, series: dict, figures: dict) -> dict:
    close, prev = series["close"][-1], series["close"][-2] if len(series["close"]) > 1 else None
    fund = {k: v for k, v in figures.items() if k != "about"}
    fund["all_time_high"], fund["all_time_low"] = round(max(series["high"]), 4), round(min(series["low"]), 4)
    sector = "ETF" if kind == "etf" else fund.get("sector") or "US stock"
    return {"symbol": symbol, "name": name, "kind": kind, "sector": sector,
            "price": round(close, 2), "change_pct": round((close / prev - 1) * 100, 2) if prev else None,
            "change": round(close - prev, 2) if prev else None,
            "volume": series["volume"][-1], "currency": "USD", "as_of": series["dates"][-1], "fund": fund,
            "metrics": metrics.row_metrics(series, close, series["volume"][-1]),
            "analysis": analytics.analyze(series["adj"][-ANALYSIS_BARS:], series["volume"][-ANALYSIS_BARS:]),
            "dividend": us.dividend_summary(series)}


def nse_overlay(row: dict, figures: dict) -> None:
    """Lays the user's CSV figures over an NSE row and derives what follows from them."""
    price = row["price"]
    fund = {k: v for k, v in figures.items() if k != "about"}
    if "market_cap" not in fund and fund.get("shares_outstanding"):
        fund["market_cap"] = fund["shares_outstanding"] * price
    if "pe" not in fund and (fund.get("eps") or 0) > 0:
        fund["pe"] = round(price / fund["eps"], 2)
    row["fund"] = fund
    m = row["metrics"]
    for key in m["perf"]:
        if f"perf_{key}_pct" in fund:
            m["perf"][key] = fund[f"perf_{key}_pct"]
    for key, column in (("high_52w", "high_52w"), ("low_52w", "low_52w"), ("rsi", "rsi"),
                        ("rel_vol", "rel_vol"), ("volatility_1m", "volatility_pct")):
        if column in fund:
            m[key] = fund[column]
    if not row.get("dividend"):
        row["dividend"] = nse_dividend({"dividend_per_share": fund.get("dividend_per_share"),
                                        "book_closure": fund.get("book_closure", "")}, price)
        if row["dividend"]:
            row["dividend"]["source"] = "config/nse_fundamentals.csv"
        elif (fund.get("div_yield_pct") or 0) > 0:
            row["dividend"] = {"yield_pct": fund["div_yield_pct"], "source": "config/nse_fundamentals.csv",
                               "annual_per_share": round(price * fund["div_yield_pct"] / 100, 4)}


def _nse_rows(config: dict, risk: str) -> tuple:
    quotes = nse.get_quotes()
    recorded = history.load_all()
    dividends = config.get("nse_dividends") or {}
    table = fundamentals.nse_table()
    rows = []
    for s in quotes["stocks"]:
        past = recorded.get(s["symbol"])
        row = {**s, "analysis": analytics.analyze(past["close"], past["volume"]) if past
               else {"bars": 0, "enough_history": False},
               "metrics": metrics.row_metrics(past, s["price"], s["volume"]),
               "dividend": nse_dividend(dividends.get(s["symbol"]), s["price"])}
        nse_overlay(row, table.get(s["symbol"], {}))
        row.update(screener.score_stock(s, risk))
        rows.append(row)
    return rows, quotes, recorded


def _check(market: str, risk: str = "balanced") -> None:
    if market not in MARKETS:
        raise ValueError(f"market must be one of {MARKETS}")
    if risk not in screener.RISKS:
        raise ValueError(f"risk must be one of {screener.RISKS}")


def market_view(market: str, risk: str = "balanced", live: bool = False) -> dict:
    _check(market, risk)
    config = load_config()
    waiting = 0
    if market == "nse":
        rows, quotes, recorded = _nse_rows(config, risk)
        status = nse.market_status()
        meta = {"label": "Kenya (NSE)", "currency": "KES", "source": quotes["source"],
                "provider": "RapidAPI Nairobi Stock Exchange feed", "updated": quotes["updated"],
                "note": quotes["note"], "status": status["message"], "is_open": status["is_open"],
                "history_days": max((len(v["dates"]) for v in recorded.values()), default=0),
                "with_fundamentals": sum(bool(r["fund"]) for r in rows)}
        # Sokowatch-Web's screener: best profile fit, available from the first day.
        top = sorted((r for r in rows if r["verdict"] == "BUY"), key=lambda r: (-r["match_score"], r["symbol"]))
        screen = [r["symbol"] for r in top[:10]]
    else:
        listed = universe(config)
        if live:
            us.refresh_latest(list(listed))
        series, stale, error = us.get_series(list(listed))
        figures, waiting = fundamentals.us_many(list(series))
        rows = [_us_row(sym, name, kind, series[sym], figures.get(sym, {}))
                for sym, (name, kind) in listed.items() if sym in series]
        missing = [s for s in listed if s not in series]
        notes = ([error] if error else []) + ([f"No data for: {', '.join(missing)}"] if missing else [])
        meta = {"label": "United States", "currency": "USD",
                "source": "unavailable" if not rows else "stale" if stale else "live",
                "provider": "Yahoo Finance (may be delayed)",
                "updated": max((r["as_of"] for r in rows), default=None),
                "note": "; ".join(notes), "status": "", "is_open": None, "history_days": None}
        ranked = sorted((r for r in rows if r["analysis"].get("buy_score") is not None),
                        key=lambda r: (-r["analysis"]["buy_score"], r["symbol"]))
        screen = [r["symbol"] for r in ranked[:10]]
    meta["fundamentals_waiting"] = waiting

    lists = analytics.picks(rows)
    lists["best_stocks"] = screen
    return {"market": market, "risk": risk, "meta": meta, "rows": rows, "picks": lists,
            "screens": screens.run(rows, market),
            "insights": screener.market_insights(rows, "NSE" if market == "nse" else "US", meta["currency"]),
            "disclaimer": analytics.DISCLAIMER}


def _candles(series: dict) -> list:
    close = series["close"]
    opens, highs, lows = (series.get(k) or close for k in ("open", "high", "low"))
    return [{"time": day, "open": round(opens[i], 4), "high": round(highs[i], 4), "low": round(lows[i], 4),
             "close": round(close[i], 4), "volume": series["volume"][i]} for i, day in enumerate(series["dates"])]


def _symbol(market: str, symbol: str) -> str:
    symbol = symbol.strip().upper()
    if market == "us" and not us.SYMBOL_RE.match(symbol):
        raise ValueError("that is not a valid US ticker")
    return symbol


def _us_series(symbol: str) -> dict:
    series, _, error = us.get_series([symbol])
    if symbol not in series:
        raise LookupError(f"no data for {symbol}" + (f": {error}" if error else ""))
    return series[symbol]


def _us_kind(config: dict, symbol: str, figures: dict) -> tuple:
    listed = universe(config).get(symbol)
    if listed:
        return listed
    # A looked-up ticker: Yahoo only reports assets under management for funds.
    return figures.get("long_name", symbol), "etf" if figures.get("aum") else "stock"


def detail(market: str, symbol: str, risk: str = "balanced", live: bool = False) -> dict:
    """One security with its full daily history. Raises LookupError for an unknown symbol."""
    _check(market, risk)
    symbol = _symbol(market, symbol)
    config = load_config()
    if market == "nse":
        rows, quotes, recorded = _nse_rows(config, risk)
        row = next((r for r in rows if r["symbol"] == symbol), None)
        if row is None:
            raise LookupError(f"no current NSE price for {symbol}")
        series = recorded.get(symbol) or {"dates": [], "close": [], "volume": []}
        about = fundamentals.nse_table().get(symbol, {}).get("about", "")
        status = nse.market_status()
        info = {"status": status["message"], "is_open": status["is_open"], "updated": quotes["updated"],
                "source": quotes["source"], "exchange": "NSE Kenya"}
    else:
        if live:
            us.refresh_latest([symbol])
        series = _us_series(symbol)
        figures = fundamentals.us_one(symbol)
        name, kind = _us_kind(config, symbol, figures)
        row = _us_row(symbol, name, kind, series, figures)
        about = figures.get("about", "")
        info = {"status": "", "is_open": None, "updated": row["as_of"], "source": "live",
                "exchange": figures.get("exchange", "US")}
    return {"market": market, "row": row, "chart": _candles(series), "ohlc": market == "us",
            "about": about, "info": info,
            "technicals": metrics.technicals(series["close"], series.get("high"), series.get("low")),
            "seasonals": metrics.seasonals(series["dates"], series["close"]),
            "speculative": analytics.speculative_reasons(row), "disclaimer": analytics.DISCLAIMER}


def intraday(market: str, symbol: str, days: str = "1d") -> dict:
    """Points for the 1-day and 5-day charts."""
    _check(market)
    if days not in INTRADAY_RANGES:
        raise ValueError(f"range must be one of {INTRADAY_RANGES}")
    symbol = _symbol(market, symbol)
    day = lambda p: datetime.fromtimestamp(p["time"], timezone.utc).date()
    if market == "us":
        series = _us_series(symbol)
        try:
            points = us.intraday(symbol, days)
        except Exception as e:
            raise LookupError(f"no intraday data for {symbol}: {str(e)[:120]}")
        first = min((day(p).isoformat() for p in points), default="")
        earlier = [c for d, c in zip(series["dates"], series["close"]) if d < first]
        return {"points": points, "previous_close": round(earlier[-1], 4) if earlier else None, "note": ""}
    quote = next((s for s in nse.get_quotes()["stocks"] if s["symbol"] == symbol), None)   # may add a point
    shift = int(nse.EAT.utcoffset(None).total_seconds())
    found = [{"time": ts + shift, "value": price, "volume": volume} for ts, price, volume in history.ticks(symbol)]
    keep = sorted({day(p) for p in found})[-(1 if days == "1d" else 5):]
    return {"points": [p for p in found if day(p) in keep],
            "previous_close": round(quote["price"] - quote["change"], 2) if quote else None,
            "note": "NSE points are the prices this bot has fetched (about one an hour while the market is open), "
                    "not every trade."}


def financials(market: str, symbol: str, quarterly: bool = False) -> dict:
    """Statements for a company, or holdings and sector mix for a fund."""
    _check(market)
    symbol = _symbol(market, symbol)
    if market == "nse":
        return {"statements": [], "fund": None, "note": "The NSE feed has no financial statements. "
                "Headline figures you add to config/nse_fundamentals.csv appear under Overview."}
    figures = fundamentals.us_one(symbol)
    _, kind = _us_kind(load_config(), symbol, figures)
    try:
        if kind == "etf":
            return {"statements": [], "fund": fundamentals.fund_profile(symbol), "note": ""}
        found = fundamentals.statements(symbol, quarterly)
    except Exception as e:
        raise LookupError(f"Yahoo returned no financials for {symbol}: {str(e)[:120]}")
    return {"statements": found, "fund": None, "note": "" if found else "No statements available."}


def news(market: str, symbol: str) -> dict:
    _check(market)
    symbol = _symbol(market, symbol)
    if market == "nse":
        quote = next((s for s in nse.get_quotes()["stocks"] if s["symbol"] == symbol), None)
        return fundamentals.nse_news(symbol, quote["name"] if quote else "")
    try:
        items = fundamentals.us_news(symbol)
    except Exception as e:
        raise LookupError(f"no news for {symbol}: {str(e)[:120]}")
    return {"items": items, "note": "" if items else "No recent headlines."}
