"""
Daily long-term investing digest: scored stock/ETF recommendations (see
long_term/stock_analysis.py), forex/crypto/gold outlook (long_term/
market_outlook.py), sell candidates, and gainers/losers across US stocks,
ETFs, and NSE Kenya — built from free data sources (yfinance,
afx.kwayisi.org) with no API key required.

Cadences, driven from long_term/scheduler.py:
  - Startup: refresh_analysis() — fills the dashboard right away (no alerts).
  - Hourly (cheap): refresh_dashboard_cache() — prices, gainers/losers and
    the forex/crypto/gold outlook; no fundamentals calls, so it's safe to run
    every hour without worrying about rate limits on the free sources.
  - Daily (heavier): run_daily_digest() — full analysis + day-over-day
    deterioration check (sell candidates) + gainers/losers, sent to
    Telegram/Discord/email and cached for the dashboard.

Persisted state (data/long_term_state.json) is what makes "sell candidates"
possible: each daily run compares today's screen/trend result for a ticker
against the last run's, and flags tickers that just started failing —
without that history, there's nothing to call a "change" against.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("daily_digest")

STATE_FILE = Path(__file__).parent.parent / "data" / "long_term_state.json"
CACHE_FILE = Path(__file__).parent.parent / "data" / "intel_cache" / "long_term_dashboard.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def build_watchlist(config: dict) -> dict:
    universe = config.get("long_term", {}).get("universe", {})
    return {
        "us_stocks": universe.get("us_stocks", []),
        "etfs": universe.get("etfs", []),
        "nse_kenya": universe.get("nse_kenya", []),
    }


def make_market_data_fn(nse_feed):
    """Returns a market_data_fn for EquityScreener.trend_context(): US/ETF
    tickers go to yfinance (free, years of history), NSE tickers go to the
    slow-building accumulated cache in data_feeds/nse_feed.py (see its
    docstring for why that's the honest ceiling on free NSE history)."""
    import time
    from data_feeds.nse_feed import DEFAULT_NSE_TICKERS
    cache = {}  # ticker -> (fetched_at, df): buy/sell/analysis passes share one download

    def _fn(ticker: str, market: str = None):
        if market == "nse" or (market is None and ticker.upper() in DEFAULT_NSE_TICKERS):
            return nse_feed.get_accumulated_history(ticker)
        hit = cache.get(ticker)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).history(period="1y", interval="1d")
            if df is None or len(df) == 0:
                return None
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]]
            cache[ticker] = (time.time(), df)
            return df
        except Exception as e:
            logger.warning(f"yfinance history fetch failed for {ticker}: {e}")
            return None

    return _fn


# ---------- gainers / losers ----------

def _us_gainers_losers(tickers: list, top_n: int) -> tuple[list, list]:
    if not tickers:
        return [], []
    try:
        import yfinance as yf
    except ImportError:
        return [], []

    try:
        data = yf.download(tickers, period="5d", progress=False,
                            group_by="ticker", threads=True, auto_adjust=True)
    except Exception as e:
        logger.warning(f"yfinance batch download failed: {e}")
        return [], []

    moves = []
    for t in tickers:
        try:
            closes = (data[t]["Close"] if len(tickers) > 1 else data["Close"]).dropna()
            if len(closes) < 2:
                continue
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if prev <= 0:
                continue
            pct = (last - prev) / prev * 100
            moves.append({"ticker": t, "price": round(last, 2), "change_pct": round(pct, 2)})
        except Exception:
            continue

    moves.sort(key=lambda m: m["change_pct"], reverse=True)
    gainers = [m for m in moves if m["change_pct"] > 0][:top_n]
    losers = sorted([m for m in moves if m["change_pct"] < 0], key=lambda m: m["change_pct"])[:top_n]
    return gainers, losers


def _nse_gainers_losers(nse_feed, top_n: int) -> tuple[list, list]:
    """Uses the whole exchange (one free request covers all ~69 listed
    tickers), not just the configured watchlist — "gainers/losers today"
    is more useful market-wide than restricted to a personal watchlist."""
    snapshot = nse_feed.get_market_snapshot()
    if not snapshot:
        return [], []
    moves = []
    for row in snapshot["universe"]:
        price, change = row.get("price"), row.get("change")
        if price is None or change is None:
            continue
        prev = price - change
        if prev <= 0:
            continue
        pct = change / prev * 100
        moves.append({"ticker": row["ticker"], "name": row["name"], "price": price, "change_pct": round(pct, 2)})
    moves.sort(key=lambda m: m["change_pct"], reverse=True)
    gainers = [m for m in moves if m["change_pct"] > 0][:top_n]
    losers = sorted([m for m in moves if m["change_pct"] < 0], key=lambda m: m["change_pct"])[:top_n]
    return gainers, losers


def compute_gainers_losers(config: dict, nse_feed, watchlist: dict) -> dict:
    top_n = config.get("long_term", {}).get("gainers_losers_top_n", 8)
    us_gainers, us_losers = _us_gainers_losers(watchlist["us_stocks"] + watchlist["etfs"], top_n)
    nse_gainers, nse_losers = _nse_gainers_losers(nse_feed, top_n)
    return {
        "us": {"gainers": us_gainers, "losers": us_losers},
        "nse": {"gainers": nse_gainers, "losers": nse_losers},
    }


# ---------- buy candidates ----------

def _trend_label(trend: dict | None, analysis_row: dict = None) -> str:
    if trend and trend.get("mode") == "full":
        return (f"{'above' if trend['above_200dma'] else 'below'} 200DMA, "
                f"{'golden cross' if trend['golden_cross'] else 'no golden cross'}, "
                f"30d momentum {trend['momentum_30d_pct']:+.1f}%")
    m = (analysis_row or {}).get("metrics", {})
    if m.get("return_1y") is not None or m.get("return_3m") is not None:
        return f"3m {m.get('return_3m') or 0:+.1f}%, 1y {m.get('return_1y') or 0:+.1f}%"
    if trend:
        return f"{trend['momentum_window_days']}d momentum {trend['momentum_pct']:+.1f}% (limited history)"
    return "no trend data yet"


def buy_candidates_from_analysis(analysis: dict) -> list[dict]:
    """Buy / Strong Buy names from stock_analysis, in the shape the dashboard's
    buy-candidates table already reads."""
    out = []
    for row in analysis.get("stocks", []) + analysis.get("etfs", []):
        if row["recommendation"] not in ("Strong Buy", "Buy"):
            continue
        out.append({"ticker": row["ticker"], "market": row["market"], "type": row["type"],
                    "recommendation": row["recommendation"], "score": row["score"],
                    "reasons": row["positives"] or [f"Score {row['score']}"],
                    "trend": _trend_label(row.get("trend"), row)})
    out.sort(key=lambda r: r["score"] or 0, reverse=True)
    return out


# ---------- sell candidates ----------

def find_sell_candidates(config: dict, screener, watchlist: dict, prior_state: dict) -> tuple[list, dict]:
    """Returns (sell_candidates, new_state). A ticker is flagged when either:
      - it passed the fundamentals screen last run and fails it now, or
      - its trend flips from above-200DMA to below (full mode only), or
      - price has dropped at least sell_review.trend_drop_pct since the
        last run that's at least sell_review.lookback_days old.
    First time a ticker is seen, it's just recorded — there's nothing to
    compare a "change" against yet."""
    sell_cfg = config.get("long_term", {}).get("sell_review", {})
    drop_threshold = sell_cfg.get("trend_drop_pct", 8)
    lookback_days = sell_cfg.get("lookback_days", 5)

    sell_candidates = []
    new_state = {}
    today = datetime.now(timezone.utc).date()

    entries = [(t, "us") for t in watchlist["us_stocks"] + watchlist["etfs"]] + \
              [(t, "nse") for t in watchlist["nse_kenya"]]

    for ticker, market in entries:
        result = screener.screen_one(ticker, market=market)
        trend = screener.trend_context(ticker, market=market)
        passed = bool(result and result["passed"])
        above_200 = trend.get("above_200dma") if trend else None
        price = trend.get("last_price") if trend else None

        prior = prior_state.get(ticker)
        reasons = []

        if prior:
            if prior.get("passed") is True and passed is False:
                reasons.append("No longer passes the fundamentals screen "
                                f"(was: {', '.join(prior.get('last_pass_reasons', [])[:2]) or 'n/a'})")
            if prior.get("above_200dma") is True and above_200 is False:
                reasons.append("Broke below its 200-day moving average")

            prior_date = prior.get("date")
            prior_price = prior.get("price")
            if prior_date and prior_price and price:
                try:
                    days_ago = (today - datetime.fromisoformat(prior_date).date()).days
                except ValueError:
                    days_ago = 0
                if days_ago >= lookback_days and prior_price > 0:
                    drop_pct = (prior_price - price) / prior_price * 100
                    if drop_pct >= drop_threshold:
                        reasons.append(f"Down {drop_pct:.1f}% over the last {days_ago} days")

        if reasons:
            sell_candidates.append({
                "ticker": ticker, "market": market or "us",
                "reasons": reasons, "price": price,
            })

        new_state[ticker] = {
            "date": today.isoformat(),
            "passed": passed,
            "last_pass_reasons": result["reasons_pass"] if result else [],
            "above_200dma": above_200,
            "price": price,
        }

    return sell_candidates, new_state


# ---------- formatting ----------

def _money(v, currency="USD"):
    if v is None:
        return "n/a"
    sym = "$" if currency == "USD" else f"{currency} "
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= div:
            return f"{sym}{v / div:.1f}{suffix}"
    return f"{sym}{v:,.0f}"


def _num(v, fmt="{:.1f}", suffix=""):
    return "n/a" if v is None else fmt.format(v) + suffix


def _stock_line(s: dict) -> str:
    m = s["metrics"]
    parts = [f"P/E {_num(m['pe_ratio'])}", f"Div {_num(m['dividend_yield'], '{:.1f}', '%')}"]
    if s["market"] == "nse":
        parts += [f"1y {_num(m['return_1y'], '{:+.0f}', '%')}",
                  f"KES {_num(s['price'], '{:,.2f}')}", f"cap {_money(s['market_cap_usd'])}"]
    else:
        parts += [f"ROE {_num(m['roe_pct'], '{:.0f}', '%')}",
                  f"upside {_num(m['analyst_upside_pct'], '{:+.0f}', '%')}",
                  f"${_num(s['price'], '{:,.2f}')}"]
    return (f"  • <b>{s['ticker']}</b> {s['recommendation']} ({s['score']:.0f}/100) — "
            + " | ".join(parts))


def format_digest(analysis: dict, outlook: dict, sell: list, movers: dict) -> str:
    from long_term.stock_analysis import top_picks
    lines = ["<b>📅 Daily Investing & Markets Digest</b>"]

    for market, title in (("us", "🇺🇸 Top US stock picks"), ("nse", "🇰🇪 Top NSE Kenya picks")):
        picks = top_picks(analysis, market, 5)
        lines.append(f"\n<b>{title}</b>")
        if picks:
            for s in picks:
                lines.append(_stock_line(s))
                if s["positives"]:
                    lines.append(f"      <i>{'; '.join(s['positives'][:2])}</i>")
        else:
            lines.append("  none rated Buy today")

    etfs = [e for e in analysis.get("etfs", []) if e["recommendation"] in ("Strong Buy", "Buy")][:4]
    if etfs:
        lines.append("\n<b>🧺 ETFs</b>")
        for e in etfs:
            m = e["metrics"]
            lines.append(f"  • <b>{e['ticker']}</b> {e['recommendation']} ({e['score']:.0f}/100) — "
                         f"fee {_num(m['expense_ratio_pct'], '{:.2f}', '%')} | "
                         f"5y {_num(m['return_5y_avg'], '{:.1f}', '%/yr')}")

    avoid = [s for s in analysis.get("stocks", []) if s["recommendation"] == "Avoid"][:5]
    if avoid:
        lines.append("\n<b>⛔ Avoid for now</b>")
        for s in avoid:
            lines.append(f"  • <b>{s['ticker']}</b> — {s['negatives'][0] if s['negatives'] else 'weak score'}")

    instruments = outlook.get("instruments", [])
    if instruments:
        lines.append("\n<b>🌍 Forex / Crypto / Gold outlook</b>")
        icon = {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "⚪"}
        for r in instruments:
            lines.append(f"  {icon.get(r['outlook'], '⚪')} <b>{r['name']}</b> {r['price']:,} "
                         f"({_num(r['change_1w'], '{:+.1f}', '%')} 1w, RSI {_num(r['rsi'], '{:.0f}')}) "
                         f"— {r['trend']}, {r['outlook']}")

    if sell:
        lines.append("\n<b>🔴 Consider selling / reviewing</b>")
        for c in sell[:10]:
            lines.append(f"  • <b>{c['ticker']}</b> — {c['reasons'][0]}")

    us, nse = movers.get("us", {}), movers.get("nse", {})
    for label, mv in (("US/ETF", us), ("NSE", nse)):
        if mv.get("gainers") or mv.get("losers"):
            lines.append(f"\n<b>📈 {label} movers today</b>")
            if mv.get("gainers"):
                lines.append("  Gainers: " + ", ".join(f"{m['ticker']} {m['change_pct']:+.1f}%" for m in mv["gainers"]))
            if mv.get("losers"):
                lines.append("  Losers: " + ", ".join(f"{m['ticker']} {m['change_pct']:+.1f}%" for m in mv["losers"]))

    lines.append("\n<i>Rules-based scores from public data (yfinance, afx.kwayisi.org) — "
                 "not financial advice or a price prediction. Full table on the dashboard.</i>")
    return "\n".join(lines)


def format_digest_email(analysis: dict, outlook: dict, sell: list) -> str:
    from alerts.notifier import _email_header, _email_footer, EMAIL_COLORS as C
    rec_color = {"Strong Buy": C["accent_green"], "Buy": "#7bd88f", "Hold": C["accent_orange"],
                 "Avoid": C["accent_red"]}
    th = f'style="text-align:left;padding:6px 8px;color:{C["text_secondary"]};font-size:11px;border-bottom:1px solid {C["border"]}"'
    td = f'style="padding:6px 8px;color:{C["text_primary"]};font-size:13px;border-bottom:1px solid {C["border"]}"'

    def table(title, headers, rows):
        head = "".join(f"<th {th}>{h}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td {td}>{c}</td>" for c in r) + "</tr>" for r in rows)
        return (f'<h3 style="color:{C["accent_purple"]};font-size:13px;letter-spacing:1px;margin:22px 0 6px">{title}</h3>'
                f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{head}</tr>{body}</table>')

    def rec(r):
        return f'<b style="color:{rec_color.get(r, C["text_secondary"])}">{r}</b>'

    parts = []
    for market, title in (("us", "US STOCKS"), ("nse", "NSE KENYA STOCKS")):
        rows = []
        for s in [s for s in analysis.get("stocks", []) if s["market"] == market][:12]:
            m = s["metrics"]
            rows.append([f"<b>{s['ticker']}</b><br><span style='color:#888;font-size:11px'>{(s['name'] or '')[:28]}</span>",
                         rec(s["recommendation"]), f"{s['score']:.0f}" if s["score"] is not None else "n/a",
                         _num(m["pe_ratio"]), _num(m["dividend_yield"], "{:.1f}", "%"),
                         _num(m["roe_pct"], "{:.0f}", "%") if market == "us" else _num(m["return_1y"], "{:+.0f}", "%"),
                         _money(s["market_cap_usd"]),
                         "; ".join(s["positives"][:2]) or "; ".join(s["negatives"][:1])])
        parts.append(table(title, ["Stock", "Call", "Score", "P/E", "Div", "ROE" if market == "us" else "1Y",
                                   "Mkt cap", "Why"], rows))

    etf_rows = [[f"<b>{e['ticker']}</b>", rec(e["recommendation"]), f"{e['score']:.0f}" if e["score"] is not None else "n/a",
                 _num(e["metrics"]["expense_ratio_pct"], "{:.2f}", "%"),
                 _num(e["metrics"]["return_5y_avg"], "{:.1f}", "%/yr"), _money(e["metrics"]["total_assets"])]
                for e in analysis.get("etfs", [])]
    if etf_rows:
        parts.append(table("ETFS", ["ETF", "Call", "Score", "Fee", "5y avg", "Assets"], etf_rows))

    outlook_color = {"Bullish": C["accent_green"], "Bearish": C["accent_red"]}
    out_rows = [[f"<b>{r['name']}</b>", f"{r['price']:,}", _num(r["change_1w"], "{:+.1f}", "%"),
                 _num(r["change_1m"], "{:+.1f}", "%"), _num(r["rsi"], "{:.0f}"), r["trend"],
                 f'<b style="color:{outlook_color.get(r["outlook"], C["accent_orange"])}">{r["outlook"]}</b>']
                for r in outlook.get("instruments", [])]
    if out_rows:
        parts.append(table("FOREX / CRYPTO / GOLD OUTLOOK", ["Market", "Price", "1w", "1m", "RSI", "Trend", "Outlook"], out_rows))

    if sell:
        parts.append(table("CONSIDER SELLING / REVIEWING", ["Ticker", "Why"],
                           [[f"<b>{c['ticker']}</b>", c["reasons"][0]] for c in sell[:10]]))

    body = "".join(parts) + (f'<p style="color:#6b7280;font-size:11px;margin-top:18px">Rules-based scores from public '
                             f'data (yfinance, afx.kwayisi.org). Not financial advice or a price prediction.</p>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:{C['bg_body']};font-family:-apple-system,Segoe UI,Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;"><tr><td align="center">
<table width="760" cellpadding="0" cellspacing="0" style="background:{C['bg_card']};border-radius:12px;border:1px solid {C['border']};">
<tr><td>{_email_header("📅 Daily Investing & Markets Digest", "Stock picks, ETFs and forex/crypto/gold outlook")}</td></tr>
<tr><td style="padding:8px 28px 24px;">{body}</td></tr>
<tr><td>{_email_footer()}</td></tr>
</table></td></tr></table></body></html>"""


# ---------- entry points (called from long_term/scheduler.py) ----------

def read_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _write_cache(result: dict):
    try:
        CACHE_FILE.write_text(json.dumps(result, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Failed to write long-term dashboard cache: {e}")


def refresh_analysis(config: dict, fundamentals, nse_feed) -> dict:
    """Full stock/ETF analysis + market outlook, cached for the dashboard (no alerts)."""
    from long_term.screener import EquityScreener
    from long_term.stock_analysis import analyze_universe
    from long_term.market_outlook import build_market_outlook

    watchlist = build_watchlist(config)
    screener = EquityScreener(config, fundamentals, make_market_data_fn(nse_feed))
    analysis = analyze_universe(screener, watchlist)
    result = {**read_cache(),
              "analysis": analysis,
              "market_outlook": build_market_outlook(config),
              "buy_candidates": buy_candidates_from_analysis(analysis),
              "updated_at": datetime.now(timezone.utc).isoformat()}
    _write_cache(result)
    return result


def run_daily_digest(config: dict, fundamentals, nse_feed, notifier):
    """Heavier daily job: full stock/ETF analysis, forex/crypto/gold outlook,
    sell-candidate deterioration check, gainers/losers — sends the
    recommendations to Telegram/Discord/email and caches for the dashboard."""
    from long_term.screener import EquityScreener
    from long_term.stock_analysis import analyze_universe
    from long_term.market_outlook import build_market_outlook

    watchlist = build_watchlist(config)
    market_data_fn = make_market_data_fn(nse_feed)
    screener = EquityScreener(config, fundamentals, market_data_fn)

    analysis = analyze_universe(screener, watchlist)
    outlook = build_market_outlook(config)
    prior_state = _load_state()
    sell, new_state = find_sell_candidates(config, screener, watchlist, prior_state)
    _save_state(new_state)

    movers = compute_gainers_losers(config, nse_feed, watchlist)

    notifier.notify_report(
        "long_term_daily_digest",
        format_digest(analysis, outlook, sell, movers),
        subject=f"📅 Daily Investing Digest — {datetime.now(timezone.utc):%d %b %Y}",
        email_html=format_digest_email(analysis, outlook, sell),
    )

    result = {
        "analysis": analysis,
        "market_outlook": outlook,
        "buy_candidates": buy_candidates_from_analysis(analysis),
        "sell_candidates": sell,
        "movers": movers,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "daily",
    }
    _write_cache(result)
    return result


def refresh_dashboard_cache(config: dict, nse_feed):
    """Cheap hourly job: prices, gainers/losers and the forex/crypto/gold
    outlook (one batched download) — no fundamentals calls, so the dashboard
    stays fresh between daily digests without burning rate limits."""
    from long_term.market_outlook import build_market_outlook

    watchlist = build_watchlist(config)
    movers = compute_gainers_losers(config, nse_feed, watchlist)

    # Preserve the last daily run's analysis/candidates — this job only
    # refreshes the price-derived parts.
    existing = read_cache()
    result = {
        **existing,
        "market_outlook": build_market_outlook(config),
        "movers": movers,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "hourly",
    }
    _write_cache(result)
    return result
