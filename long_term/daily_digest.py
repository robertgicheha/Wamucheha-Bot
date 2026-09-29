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
  - Daily 16:00 EAT: refresh_nse_dashboard() — the whole listed NSE exchange
    (every security, not a 20-name watchlist) with fundamentals, P/E, a chart
    series and a price projection per ticker, cached separately for the
    dashboard's NSE panel.

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
NSE_CACHE_FILE = Path(__file__).parent.parent / "data" / "intel_cache" / "nse_dashboard.json"
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


def format_digest_email(analysis: dict, outlook: dict, sell: list,
                        movers: dict = None) -> str:
    """HTML email for the digest. Email gets the wide layout and the full data
    set (every column, not the Telegram top-5 cut) because it is read once and
    kept; Telegram/Discord are read in a live channel where brevity wins."""
    from alerts.notifier import (
        _email_header, _email_footer, _build_email_body, _stat_tiles, _score_bar,
        _score_color, _esc, EMAIL_COLORS as C,
    )
    from long_term.stock_analysis import top_picks

    movers = movers or {}
    rec_color = {"Strong Buy": C["accent_green"], "Buy": "#7bd88f", "Hold": C["accent_orange"],
                 "Avoid": C["accent_red"]}
    th = f'style="text-align:left;padding:7px 8px;color:{C["text_secondary"]};font-size:10px;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;border-bottom:1px solid {C["border"]};white-space:nowrap;"'
    td = f'style="padding:7px 8px;color:{C["text_primary"]};font-size:12px;border-bottom:1px solid {C["border"]};vertical-align:top;"'
    td_n = f'style="padding:7px 8px;color:{C["text_secondary"]};font-size:12px;border-bottom:1px solid {C["border"]};white-space:nowrap;"'

    def table(title, headers, rows, empty="No qualifying names today."):
        if not rows:
            return ""
        head = "".join(f"<th {th}>{_esc(h)}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f'<td {td}>{c}</td>' if isinstance(c, str) and c.startswith("<")
                             else f'<td {td_n}>{c}</td>' for c in r) + "</tr>"
            for r in rows)
        return (f'<h3 style="color:{C["accent_purple"]};font-size:12px;font-weight:700;'
                f'letter-spacing:1.4px;margin:24px 0 8px;">{_esc(title)}</h3>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                f'<tr>{head}</tr>{body}</table>')

    def rec(r):
        return f'<b style="color:{rec_color.get(r, C["text_secondary"])};">{_esc(r)}</b>'

    def pct_cell(v, fmt="{:+.1f}"):
        """Coloured change. Absent data stays neutral grey rather than
        borrowing the wrong direction's colour."""
        if v is None:
            return '<span style="color:#6b7280;">n/a</span>'
        color = C["accent_green"] if v > 0 else C["accent_red"] if v < 0 else C["text_secondary"]
        return f'<span style="color:{color};font-weight:600;">{fmt.format(v)}</span>'

    stocks = analysis.get("stocks", [])
    etfs = analysis.get("etfs", [])
    instruments = outlook.get("instruments", [])

    us_picks = top_picks(analysis, "us", 3)
    nse_picks = top_picks(analysis, "nse", 3)
    buys = [s for s in stocks if s["recommendation"] in ("Strong Buy", "Buy")]
    best = max(buys, key=lambda s: s["score"] or 0, default=None)
    avoid = [s for s in stocks if s["recommendation"] == "Avoid"]
    bullish = sum(1 for r in instruments if r["outlook"] == "Bullish")
    bearish = sum(1 for r in instruments if r["outlook"] == "Bearish")

    # ── Hero: the one-glance summary ──────────────────────────────────────
    rows = ""
    rows += _stat_tiles([
        (str(len(buys)), "buy-rated", C["accent_green"]),
        (str(len(avoid)), "avoid", C["accent_red"]),
        (f"{bullish}/{bearish}", "bull / bear", C["accent_cyan"]),
        (str(len(etfs)), "etfs", C["accent_purple"]),
    ])

    # ── Spotlight: highest-conviction name, given room to breathe ─────────
    if best:
        bm = best["metrics"]
        detail = (f'P/E {_num(bm["pe_ratio"])} &nbsp;·&nbsp; '
                  f'Div {_num(bm["dividend_yield"], "{:.1f}", "%")} &nbsp;·&nbsp; '
                  + (f'ROE {_num(bm["roe_pct"], "{:.0f}", "%")} &nbsp;·&nbsp; '
                     f'upside {_num(bm.get("analyst_upside_pct"), "{:+.0f}", "%")}'
                     if best["market"] == "us" else
                     f'1y {_num(bm.get("return_1y"), "{:+.0f}", "%")} &nbsp;·&nbsp; '
                     f'KES {_num(best.get("price"), "{:,.2f}")}'))
        rows += f"""
    <tr><td style="padding:6px 0 4px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{C['bg_tile']};border:1px solid {C['border']};border-left:3px solid {_score_color(best['score'])};border-radius:10px;">
        <tr><td style="padding:16px 18px;">
          <div style="color:{C['accent_purple']};font-size:10px;font-weight:700;letter-spacing:1.4px;">TOP CONVICTION PICK</div>
          <div style="padding:5px 0 3px;">
            <span style="color:#fff;font-size:21px;font-weight:800;letter-spacing:-0.3px;">{_esc(best['ticker'])}</span>
            <span style="color:{C['text_secondary']};font-size:13px;padding-left:8px;">{_esc((best.get('name') or '')[:38])}</span>
          </div>
          <div style="padding:0 0 9px;">
            {rec(best['recommendation'])} &nbsp;
            <span style="color:{C['text_secondary']};font-size:12px;">{_money(best.get('market_cap_usd'))} cap</span>
          </div>
          {_score_bar(best['score'])}
          <div style="padding-top:9px;color:{C['text_secondary']};font-size:12px;line-height:1.5;">{detail}</div>
        </td></tr>
      </table>
    </td></tr>"""

    parts = []

    # ── Runners-up, the usual one-line-each treatment ─────────────────────
    spotlight = []
    for market, flag, title in (("us", "🇺🇸", "TOP US PICKS"), ("nse", "🇰🇪", "TOP NSE KENYA PICKS")):
        picks = [s for s in (us_picks if market == "us" else nse_picks) if s is not best]
        sub = []
        for s in picks[:3]:
            m = s["metrics"]
            why = (f'P/E {_num(m["pe_ratio"])} · ROE {_num(m["roe_pct"], "{:.0f}", "%")}'
                   if market == "us" else
                   f'P/E {_num(m["pe_ratio"])} · 1y {_num(m.get("return_1y"), "{:+.0f}", "%")}')
            sub.append(f"""
        <tr><td style="padding:9px 0;border-bottom:1px solid {C['border']};">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="padding-right:12px;vertical-align:middle;width:78px;">
              <span style="color:#fff;font-size:14px;font-weight:700;">{_esc(s['ticker'])}</span>
            </td>
            <td style="vertical-align:middle;width:74px;">{rec(s['recommendation'])}</td>
            <td style="vertical-align:middle;width:64px;">{_score_bar(s['score'], width=40)}</td>
            <td style="vertical-align:middle;color:{C['text_secondary']};font-size:11px;">{_esc(why)}</td>
          </tr></table>
        </td></tr>""")
        if sub:
            spotlight.append(f'<h3 style="color:{C["accent_purple"]};font-size:12px;font-weight:700;'
                             f'letter-spacing:1.4px;margin:22px 0 4px;">{flag} {_esc(title)}</h3>'
                             f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                             + "".join(sub) + "</table>")
    if spotlight:
        rows += '<tr><td colspan="2" style="padding:0;">' + "".join(spotlight) + "</td></tr>"

    # ── Full ranked tables ────────────────────────────────────────────────
    for market, flag, title in (("us", "🇺🇸", "ALL US STOCKS"), ("nse", "🇰🇪", "ALL NSE KENYA STOCKS")):
        table_rows = []
        ranked = sorted([s for s in stocks if s["market"] == market],
                        key=lambda s: (s["score"] or 0), reverse=True)[:12]
        for s in ranked:
            m = s["metrics"]
            table_rows.append([
                f'<b style="color:#fff;">{_esc(s["ticker"])}</b>'
                f'<br><span style="color:#7b7d90;font-size:10px;">{_esc((s.get("name") or "")[:30])}</span>',
                rec(s["recommendation"]),
                _score_bar(s["score"], width=42),
                _num(m["pe_ratio"]),
                _num(m["dividend_yield"], "{:.1f}", "%"),
                _num(m["roe_pct"], "{:.0f}", "%") if market == "us"
                else pct_cell(m.get("return_1y"), "{:+.0f}"),
                _money(s.get("market_cap_usd")),
                _esc("; ".join(s["positives"][:2]) or "; ".join(s["negatives"][:1])),
            ])
        parts.append(table(f"{flag} {title}",
                           ["Stock", "Call", "Score", "P/E", "Div", "ROE" if market == "us" else "1Y",
                            "Mkt cap", "Why"], table_rows))

    etf_rows = []
    for e in sorted(etfs, key=lambda e: (e["score"] or 0), reverse=True):
        m = e["metrics"]
        etf_rows.append([
            f'<b style="color:#fff;">{_esc(e["ticker"])}</b>'
            f'<br><span style="color:#7b7d90;font-size:10px;">{_esc((e.get("name") or "")[:30])}</span>',
            rec(e["recommendation"]),
            _score_bar(e["score"], width=42),
            _num(m["expense_ratio_pct"], "{:.2f}", "%"),
            _num(m["return_5y_avg"], "{:.1f}", "%/yr"),
            _money(m.get("total_assets")),
        ])
    parts.append(table("🧺 ETFs", ["ETF", "Call", "Score", "Fee", "5y avg", "Assets"], etf_rows))

    # ── Macro: adds RSI state and 1m alongside the existing 1w ────────────
    out_rows = []
    for r in sorted(instruments, key=lambda r: (r.get("change_1w") or 0), reverse=True):
        rsi = r.get("rsi")
        rsi_color = (C["accent_red"] if rsi is not None and rsi >= 70
                     else C["accent_green"] if rsi is not None and rsi <= 30
                     else C["text_secondary"])
        outlook_color = {"Bullish": C["accent_green"], "Bearish": C["accent_red"]}.get(
            r.get("outlook"), C["accent_orange"])
        out_rows.append([
            f'<b style="color:#fff;">{_esc(r["name"])}</b>',
            f'{r["price"]:,}' if r.get("price") is not None else "n/a",
            pct_cell(r.get("change_1w")),
            pct_cell(r.get("change_1m")),
            f'<span style="color:{rsi_color};font-weight:600;">{_num(rsi, "{:.0f}")}</span>',
            _esc(r.get("trend", "")),
            f'<b style="color:{outlook_color};">{_esc(r.get("outlook", ""))}</b>',
        ])
    parts.append(table("🌍 FOREX / CRYPTO / GOLD OUTLOOK",
                       ["Market", "Price", "1w", "1m", "RSI", "Trend", "Outlook"], out_rows))

    if sell:
        parts.append(table("🔴 CONSIDER SELLING / REVIEWING", ["Ticker", "Why"],
                           [[f'<b style="color:{C["accent_red"]};">{_esc(c["ticker"])}</b>', _esc(c["reasons"][0])]
                            for c in sell[:10]]))

    # Movers were Telegram-only before — the single biggest gap for a reader
    # who wants context on why today's scores moved.
    for label, mv in (("US / ETF", movers.get("us", {})), ("NSE", movers.get("nse", {}))):
        mover_rows = []
        for m in list(mv.get("gainers", []))[:5]:
            mover_rows.append([
                f'<b style="color:#fff;">{_esc(m["ticker"])}</b>',
                pct_cell(m.get("change_pct")),
                f'<span style="color:{C["accent_green"]};font-size:11px;font-weight:600;">GAINER</span>',
            ])
        for m in list(mv.get("losers", []))[:5]:
            mover_rows.append([
                f'<b style="color:#fff;">{_esc(m["ticker"])}</b>',
                pct_cell(m.get("change_pct")),
                f'<span style="color:{C["accent_red"]};font-size:11px;font-weight:600;">LOSER</span>',
            ])
        parts.append(table(f"📈 {label} MOVERS TODAY", ["Ticker", "Change", ""], mover_rows))

    if avoid:
        parts.append(table("⛔ AVOID FOR NOW", ["Ticker", "Why"],
                           [[f'<b style="color:#fff;">{_esc(s["ticker"])}</b>',
                             _esc(s["negatives"][0] if s.get("negatives") else "weak score")]
                            for s in avoid[:6]]))

    if parts:
        rows += ('<tr><td colspan="2" style="padding:0;">'
                 + '<h3 style="color:#6b7280;font-size:11px;font-weight:700;letter-spacing:1.6px;'
                   'margin:24px 0 0;padding-top:20px;border-top:1px solid '
                 + C["border"] + ';">FULL RANKED UNIVERSE</h3>'
                 + "".join(parts)
                 + "</td></tr>")

    body = (f'<p style="color:{C["text_secondary"]};font-size:12px;line-height:1.7;margin:14px 0 0;'
            f'padding-top:16px;border-top:1px solid {C["border"]};">'
            f'Rules-based scores computed from public data (yfinance, afx.kwayisi.org). '
            f'Not financial advice and not a price prediction. Scores are relative to each '
            f'market&rsquo;s own universe, so a 60 on NSE and a 60 on the US are not directly comparable. '
            f'Full tables and history on the dashboard.</p>')

    if best:
        preheader = (f"Top pick {best['ticker']} ({best['recommendation']}, {best['score']:.0f}/100) — "
                     f"{len(buys)} buy-rated, {len(avoid)} to avoid, {bullish} markets bullish")
    else:
        preheader = "No stocks met the buy threshold today — here is the full ranked universe"

    header = _email_header(
        "Daily Investing & Markets Digest",
        f"{datetime.now(timezone.utc):%A, %d %B %Y} · scored universe, macro outlook and movers",
        badge="Daily Briefing",
    )
    footer = _email_footer()
    return _build_email_body(header, rows, footer + body, preheader=preheader, width=760)



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
        subject=f"📅 Daily Investing Digest — {datetime.now(timezone.utc):%a %d %b %Y}",
        email_html=format_digest_email(analysis, outlook, sell, movers),
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


# ---------- NSE Kenya: whole-exchange dashboard ----------

def read_nse_cache() -> dict:
    if NSE_CACHE_FILE.exists():
        try:
            return json.loads(NSE_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _write_nse_cache(result: dict):
    try:
        NSE_CACHE_FILE.write_text(json.dumps(result, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Failed to write NSE dashboard cache: {e}")


def _chart_series(history, points: int = 120) -> dict:
    """Last `points` closes for the dashboard chart, as parallel date/close
    lists. Returns empty lists rather than None so the chart JS can bind to
    them unconditionally."""
    if history is None or len(history) == 0 or "close" not in history:
        return {"dates": [], "closes": [], "volumes": []}
    tail = history.tail(points)
    return {
        "dates": [str(d)[:10] for d in tail.index],
        "closes": [None if c != c else float(c) for c in tail["close"]],  # NaN -> null
        "volumes": [int(v) if v == v else 0 for v in tail.get("volume", [])],
    }


def _enrich_nse_stock(stock: dict, fundamentals, nse_feed) -> dict:
    """Merge a RapidAPI quote with AFX fundamentals, the existing scorer, the
    accumulated chart series, and a price projection for one NSE security.

    Every field is independently optional: RapidAPI has no fundamentals, AFX
    may not have a page for a newly listed name, and the forecast needs
    accumulated history. A missing piece is reported as null, never faked."""
    from long_term.stock_analysis import analyze_stock, analyze_etf
    from long_term.nse_forecast import build_forecast, direction_label

    ticker = stock["ticker"]
    row = {
        "ticker": ticker,
        "name": stock.get("name") or ticker,
        "sector": stock.get("sector"),
        "isin": stock.get("isin"),
        "price": stock.get("price"),
        "change_pct": stock.get("change_pct"),
        "volume": stock.get("volume"),
        "stale": bool(stock.get("stale")),
        "pe_ratio": None,
        "eps": None,
        "dividend_yield": None,
        "payout_ratio": None,
        "market_cap": None,
        "market_cap_usd": None,
        "dividend_per_share": None,
        "return_1y": None,
        "recommendation": None,
        "score": None,
        "coverage": None,
        "positives": [],
        "negatives": [],
        "trend": None,
        "forecast": None,
        "chart": {"dates": [], "closes": [], "volumes": []},
    }

    history = nse_feed.get_accumulated_history(ticker)
    row["chart"] = _chart_series(history)

    # Price projection from the accumulated closes. Available=False carries its
    # own reason string, which the dashboard renders verbatim.
    forecast = build_forecast(history)
    forecast["label"] = direction_label(forecast)
    row["forecast"] = forecast

    profile = None
    try:
        profile = fundamentals.get_profile(ticker, market="nse")
    except Exception as e:
        logger.warning(f"NSE fundamentals failed for {ticker}: {e}")

    if profile:
        row["pe_ratio"] = profile.get("pe_ratio")
        row["eps"] = profile.get("eps")
        row["dividend_yield"] = profile.get("dividend_yield")
        row["payout_ratio"] = profile.get("payout_ratio")
        row["market_cap"] = profile.get("market_cap")
        row["market_cap_usd"] = profile.get("market_cap_usd")
        row["dividend_per_share"] = profile.get("dividend_per_share")
        row["return_1y"] = profile.get("return_1y")
        row["sector"] = row["sector"] or profile.get("sector")

        # Reuse the same scorer the weekly screen and the digest use, so the
        # NSE panel can't quietly disagree with the rest of the product.
        try:
            is_etf = profile.get("quote_type") == "ETF"
            trend = analyze_etf(profile, None) if is_etf else analyze_stock(profile, None)
            row["recommendation"] = trend.get("recommendation")
            row["score"] = trend.get("score")
            row["coverage"] = trend.get("coverage")
            row["positives"] = trend.get("positives") or []
            row["negatives"] = trend.get("negatives") or []
        except Exception as e:
            logger.warning(f"NSE scoring failed for {ticker}: {e}")
    else:
        row["error"] = "No fundamentals available for this security yet."

    return row


def refresh_nse_dashboard(config: dict, fundamentals, nse_feed, max_workers: int = 4) -> dict:
    """Build the whole-exchange NSE panel: every listed security with its
    price, P/E, score, chart series and price projection.

    Runs once per trading day at 16:00 EAT, right after
    NSEFeed.refresh_market_snapshot() has written the day's close. The
    per-ticker fundamentals pass hits the free afx.kwayisi.org site (no quota,
    no key) with a small thread pool; it is bounded because one thread per
    ticker would be rude to a free third-party service.

    Reads and writes data/intel_cache/nse_dashboard.json, which the dashboard
    process serves. Nothing here calls RapidAPI — the snapshot on disk was
    already paid for by the 16:00 job."""
    from concurrent.futures import ThreadPoolExecutor

    nse_cfg = config.get("nse", {})
    notes: list[str] = []

    snapshot = nse_feed.get_market_snapshot()
    if not snapshot:
        notes.append("No NSE snapshot available — RapidAPI not configured and "
                     "afx.kwayisi.org was unreachable.")
        logger.warning("refresh_nse_dashboard: no NSE snapshot available")
        result = {"updated_at": datetime.now(timezone.utc).isoformat(),
                  "stocks": [], "counts": {"total": 0}, "notes": notes}
        _write_nse_cache(result)
        return result

    universe = snapshot.get("universe") or []
    # The AFX fallback has no `stale` flag; treat a missing volume as unknown
    # rather than as a suspended name.
    stocks = [{"ticker": s["ticker"], "name": s.get("name"), "sector": s.get("sector"),
               "isin": s.get("isin"), "price": s.get("price"),
               "change_pct": s.get("change_pct"), "volume": s.get("volume"),
               "stale": bool(s.get("stale"))}
              for s in universe]

    workers = max(1, min(max_workers, nse_cfg.get("fundamentals_max_workers", 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(
            lambda s: _enrich_nse_stock(s, fundamentals, nse_feed), stocks))

    rows.sort(key=lambda r: -(r.get("change_pct") if r.get("change_pct") is not None else -1e9))

    with_fundamentals = sum(1 for r in rows if r.get("pe_ratio") is not None
                            or r.get("eps") is not None)
    forecasts = [r for r in rows if (r.get("forecast") or {}).get("available")]
    if not forecasts:
        notes.append("No price projections yet: the daily close history is still "
                     "short. The RapidAPI history endpoints are Pro-only, so the "
                     "series builds up one close per trading day from the first run.")
    if snapshot.get("source") == "afx.kwayisi.org":
        notes.append("Showing the free afx.kwayisi.org fallback; no RapidAPI "
                     "snapshot has been persisted yet.")
    if snapshot.get("stale"):
        notes.append("The persisted RapidAPI snapshot is more than 36h old "
                     "(weekend or public holiday). Prices may not be current.")

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "trading_date": snapshot.get("trading_date"),
        "fetched_at": snapshot.get("fetched_at"),
        "source": snapshot.get("source"),
        "stale": bool(snapshot.get("stale")),
        "counts": {
            "total": len(rows),
            "with_fundamentals": with_fundamentals,
            "with_forecasts": len(forecasts),
            "stale_quotes": sum(1 for r in rows if r.get("stale")),
        },
        "sectors": snapshot.get("sectors") or [],
        "gainers": [r["ticker"] for r in rows[:8]],
        "losers": [r["ticker"] for r in rows[-8:][::-1]],
        "stocks": rows,
        "notes": notes,
        "disclaimer": "Projections are statistical extrapolations, not "
                      "recommendations. NSE Kenya is analysis-only — there is no "
                      "automated execution.",
    }
    _write_nse_cache(result)
    logger.info(f"NSE dashboard cache: {len(rows)} securities, "
                f"{with_fundamentals} with fundamentals, {len(forecasts)} with projections")
    return result
