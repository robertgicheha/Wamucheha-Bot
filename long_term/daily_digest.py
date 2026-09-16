"""
Daily long-term investing digest: buy candidates, sell candidates, and
gainers/losers across US stocks, ETFs, and NSE Kenya — built from free data
sources (yfinance, afx.kwayisi.org) with no API key required.

Two cadences, both driven from long_term/scheduler.py:
  - Hourly (cheap): refresh_dashboard_cache() — prices + gainers/losers only,
    no fundamentals calls, so it's safe to run every hour without worrying
    about rate limits on the free sources.
  - Daily (heavier): run_daily_digest() — full fundamentals screen (buy
    candidates) + day-over-day deterioration check (sell candidates) +
    gainers/losers, sent to Telegram/Discord/email and cached for the
    dashboard.

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
    from data_feeds.nse_feed import DEFAULT_NSE_TICKERS

    def _fn(ticker: str):
        if ticker.upper() in DEFAULT_NSE_TICKERS:
            return nse_feed.get_accumulated_history(ticker)
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).history(period="1y", interval="1d")
            if df is None or len(df) == 0:
                return None
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            return df[["open", "high", "low", "close", "volume"]]
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


# ---------- trend helpers ----------

def _trend_label(trend: dict | None) -> str:
    if not trend:
        return "no trend data yet"
    if trend["mode"] == "full":
        return (f"{'above' if trend['above_200dma'] else 'below'} 200DMA, "
                f"{'golden cross' if trend['golden_cross'] else 'no golden cross'}, "
                f"30d momentum {trend['momentum_30d_pct']:+.1f}%")
    return f"{trend['momentum_window_days']}d momentum {trend['momentum_pct']:+.1f}% (limited history)"


def _trend_is_bullish(trend: dict | None, lenient: bool = False) -> bool:
    """lenient=True: no trend data yet doesn't block a candidate (used for
    NSE, where history is still accumulating for free — see nse_feed.py)."""
    if not trend:
        return lenient
    if trend["mode"] == "full":
        return bool(trend["above_200dma"]) and trend["momentum_30d_pct"] > -5
    return trend["momentum_pct"] > 0


# ---------- buy candidates ----------

def find_buy_candidates(screener, watchlist: dict) -> list[dict]:
    candidates = []

    for ticker in watchlist["us_stocks"]:
        result = screener.screen_one(ticker)
        if not result or not result["passed"]:
            continue
        trend = screener.trend_context(ticker)
        if _trend_is_bullish(trend):
            candidates.append({"ticker": ticker, "market": "us", "type": "stock",
                                "reasons": result["reasons_pass"], "trend": _trend_label(trend)})

    for ticker in watchlist["nse_kenya"]:
        result = screener.screen_one(ticker, market="nse")
        if not result or not result["passed"]:
            continue
        trend = screener.trend_context(ticker)
        if _trend_is_bullish(trend, lenient=True):
            candidates.append({"ticker": ticker, "market": "nse", "type": "stock",
                                "reasons": result["reasons_pass"], "trend": _trend_label(trend)})

    # ETFs: fundamentals screening (P/E, dividend growth years, payout
    # ratio) doesn't fit a broad index fund the way it fits a company —
    # trend/momentum alone drives the ETF buy read.
    for ticker in watchlist["etfs"]:
        trend = screener.trend_context(ticker)
        if _trend_is_bullish(trend):
            candidates.append({"ticker": ticker, "market": "us", "type": "etf",
                                "reasons": [f"Trend: {_trend_label(trend)}"], "trend": _trend_label(trend)})

    return candidates


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

    entries = [(t, None) for t in watchlist["us_stocks"] + watchlist["etfs"]] + \
              [(t, "nse") for t in watchlist["nse_kenya"]]

    for ticker, market in entries:
        result = screener.screen_one(ticker, market=market)
        trend = screener.trend_context(ticker)
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

def format_digest(buy: list, sell: list, movers: dict) -> str:
    lines = ["<b>📅 Daily Long-Term Investing Digest</b>"]

    if buy:
        lines.append("\n<b>🟢 Buy candidates</b>")
        for c in buy[:15]:
            tag = "NSE" if c["market"] == "nse" else c["type"].upper()
            lines.append(f"  • <b>{c['ticker']}</b> [{tag}] — {c['reasons'][0] if c['reasons'] else ''}")
    else:
        lines.append("\n<b>🟢 Buy candidates:</b> none passed today's screen.")

    if sell:
        lines.append("\n<b>🔴 Consider selling / reviewing</b>")
        for c in sell[:15]:
            lines.append(f"  • <b>{c['ticker']}</b> — {c['reasons'][0]}")
    else:
        lines.append("\n<b>🔴 Consider selling:</b> nothing flagged today.")

    us = movers.get("us", {})
    nse = movers.get("nse", {})
    if us.get("gainers") or us.get("losers"):
        lines.append("\n<b>📈 US/ETF movers today</b>")
        if us.get("gainers"):
            lines.append("  Gainers: " + ", ".join(f"{m['ticker']} {m['change_pct']:+.1f}%" for m in us["gainers"]))
        if us.get("losers"):
            lines.append("  Losers: " + ", ".join(f"{m['ticker']} {m['change_pct']:+.1f}%" for m in us["losers"]))

    if nse.get("gainers") or nse.get("losers"):
        lines.append("\n<b>📈 NSE movers today</b>")
        if nse.get("gainers"):
            lines.append("  Gainers: " + ", ".join(f"{m['ticker']} {m['change_pct']:+.1f}%" for m in nse["gainers"]))
        if nse.get("losers"):
            lines.append("  Losers: " + ", ".join(f"{m['ticker']} {m['change_pct']:+.1f}%" for m in nse["losers"]))

    lines.append("\n<i>Screen + trend context only — not a prediction. "
                  "See docs/COMMON_MISTAKES.md #11.</i>")
    return "\n".join(lines)


# ---------- entry points (called from long_term/scheduler.py) ----------

def run_daily_digest(config: dict, fundamentals, nse_feed, notifier):
    """Heavier daily job: full fundamentals screen, sell-candidate
    deterioration check, gainers/losers — notifies and caches for the
    dashboard."""
    from long_term.screener import EquityScreener

    watchlist = build_watchlist(config)
    market_data_fn = make_market_data_fn(nse_feed)
    screener = EquityScreener(config, fundamentals, market_data_fn)

    buy = find_buy_candidates(screener, watchlist)
    prior_state = _load_state()
    sell, new_state = find_sell_candidates(config, screener, watchlist, prior_state)
    _save_state(new_state)

    movers = compute_gainers_losers(config, nse_feed, watchlist)

    message = format_digest(buy, sell, movers)
    notifier.notify("long_term_daily_digest", message)

    result = {
        "buy_candidates": buy,
        "sell_candidates": sell,
        "movers": movers,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "daily",
    }
    try:
        CACHE_FILE.write_text(json.dumps(result, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Failed to write long-term dashboard cache: {e}")
    return result


def refresh_dashboard_cache(config: dict, nse_feed):
    """Cheap hourly job: prices + gainers/losers only (no fundamentals
    calls), so the dashboard has fresh-ish data between daily digests
    without burning through free-tier/rate-limit budget."""
    watchlist = build_watchlist(config)
    movers = compute_gainers_losers(config, nse_feed, watchlist)

    # Preserve the last daily run's buy/sell candidates if present — this
    # job only refreshes the price-derived parts.
    existing = {}
    if CACHE_FILE.exists():
        try:
            existing = json.loads(CACHE_FILE.read_text())
        except Exception:
            existing = {}

    result = {
        "buy_candidates": existing.get("buy_candidates", []),
        "sell_candidates": existing.get("sell_candidates", []),
        "movers": movers,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "hourly",
    }
    try:
        CACHE_FILE.write_text(json.dumps(result, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Failed to write long-term dashboard cache: {e}")
    return result
