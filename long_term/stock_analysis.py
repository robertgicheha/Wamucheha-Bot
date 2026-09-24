"""
Stock & ETF analysis: scores every name in the long-term universe (US
stocks, ETFs, NSE Kenya) on six transparent pillars and turns the result
into a Strong Buy / Buy / Hold / Avoid call with the reasons behind it.

Pillars (0-100 each, weighted into one score):
  value 25%          P/E, P/B, EV/EBITDA, PEG, analyst upside
  profitability 25%  ROE, ROA, net & operating margin (NSE: EPS > 0 only)
  income 15%         dividend yield, payout sustainability, dividend growth streak
  growth 15%         revenue & earnings growth
  health 10%         debt/equity, current ratio, free cash flow
  momentum 10%       200DMA / golden cross / 30d momentum, or 3m/1y returns (NSE)

A pillar with no data is left out and the weights re-normalised; `coverage`
says how much of the model the data actually covered. NSE's free source has
no margins/ROE/balance sheet, so NSE coverage is lower — shown, not hidden.

This is a rules-based screen, not financial advice or a price prediction.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("stock_analysis")

WEIGHTS = {"value": 0.25, "profitability": 0.25, "income": 0.15,
           "growth": 0.15, "health": 0.10, "momentum": 0.10}
ETF_WEIGHTS = {"cost": 0.25, "returns": 0.35, "income": 0.15, "size": 0.10, "momentum": 0.15}
RATINGS = ["Avoid", "Hold", "Buy", "Strong Buy"]


def _scale(v, bad, good):
    """Map v linearly onto 0..100 where `bad`->0 and `good`->100 (either direction), clamped."""
    if v is None:
        return None
    t = (float(v) - bad) / (good - bad)  # float(): numpy scalars aren't JSON-serialisable
    return round(max(0.0, min(1.0, t)) * 100, 1)


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _weighted(scores: dict, weights: dict):
    used = {k: w for k, w in weights.items() if scores.get(k) is not None}
    total = sum(used.values())
    if not total:
        return None, 0.0
    score = sum(scores[k] * w for k, w in used.items()) / total
    return round(score, 1), round(total / sum(weights.values()), 2)


def _rating(score, coverage):
    if score is None or coverage < 0.35:
        return "Insufficient data"
    if score >= 70:
        return "Strong Buy"
    if score >= 58:
        return "Buy"
    if score >= 45:
        return "Hold"
    return "Avoid"


def _downgrade(rating):
    return RATINGS[max(0, RATINGS.index(rating) - 1)] if rating in RATINGS else rating


# ---------- stocks ----------

def analyze_stock(profile: dict, trend: dict | None) -> dict:
    p = profile
    pos, neg = [], []
    pe, pb, ev, peg = p.get("pe_ratio"), p.get("pb_ratio"), p.get("ev_to_ebitda"), p.get("peg_ratio")
    eps = p.get("eps")

    # --- value ---
    pe_score = (0.0 if pe is not None and pe <= 0 else _scale(pe, 40, 8))
    if eps is not None and eps <= 0:
        pe_score = 0.0
    if pe is not None and 0 < pe < 3:
        pe_score = 50.0  # usually one-off gains, not a real bargain
        neg.append(f"P/E {pe:.1f} is unusually low — often one-off gains; check the accounts")
    value = _avg([pe_score, _scale(pb, 8, 1), _scale(ev, 25, 6), _scale(peg, 3, 0.8),
                  _scale(p.get("analyst_upside_pct"), -10, 25)])
    if pe and pe >= 3:
        if pe < 15:
            pos.append(f"Cheap on earnings: P/E {pe:.1f}")
        elif pe > 35:
            neg.append(f"Expensive: P/E {pe:.1f}")
    if pb is not None and pb < 1.5:
        pos.append(f"Trades near book value (P/B {pb:.2f})")
    if p.get("analyst_upside_pct") is not None and p.get("analyst_count"):
        up = p["analyst_upside_pct"]
        (pos if up >= 10 else neg if up < 0 else []).append(
            f"Analyst target {up:+.0f}% vs price ({p['analyst_count']} analysts, {p.get('analyst_rating') or 'n/a'})")

    # --- profitability ---
    if p.get("market") == "nse":
        profitability = None if eps is None else (65.0 if eps > 0 else 0.0)
    else:
        profitability = _avg([_scale(p.get("roe_pct"), 0, 25), _scale(p.get("roa_pct"), 0, 10),
                              _scale(p.get("profit_margin_pct"), 0, 25),
                              _scale(p.get("operating_margin_pct"), 0, 30)])
    if eps is not None and eps <= 0:
        neg.append("Loss-making (EPS ≤ 0)")
    roe = p.get("roe_pct")
    if roe is not None:
        (pos if roe >= 15 else neg if roe < 5 else []).append(f"Return on equity {roe:.0f}%")
    pm = p.get("profit_margin_pct")
    if pm is not None:
        (pos if pm >= 15 else neg if pm < 3 else []).append(f"Net profit margin {pm:.0f}%")

    # --- income (only for dividend payers) ---
    dy, payout, years = p.get("dividend_yield"), p.get("payout_ratio"), p.get("dividend_growth_years")
    income = None
    if dy:
        if payout is None:
            payout_score = None
        elif payout <= 0:
            payout_score = 30.0
        elif payout <= 75:
            payout_score = 100.0
        else:
            payout_score = _scale(payout, 110, 75)
        income = _avg([_scale(dy, 0, 7), payout_score, _scale(years, 0, 10)])
        if dy >= 4:
            pos.append(f"High dividend yield {dy:.2f}%")
        if payout is not None and payout > 90:
            neg.append(f"Payout ratio {payout:.0f}% — dividend may be at risk")
        if years and years >= 5:
            pos.append(f"Dividend raised {years} years in a row")

    # --- growth ---
    rg, eg = p.get("revenue_growth_pct"), p.get("earnings_growth_pct")
    growth = _avg([_scale(rg, -5, 20), _scale(eg, -10, 25)])
    if rg is not None:
        (pos if rg >= 10 else neg if rg < 0 else []).append(f"Revenue growth {rg:+.0f}% YoY")
    if eg is not None and eg < -10:
        neg.append(f"Earnings falling {eg:.0f}% YoY")

    # --- financial health ---
    de, cr, fcf = p.get("debt_to_equity"), p.get("current_ratio"), p.get("free_cash_flow")
    health = _avg([_scale(de, 250, 30), _scale(cr, 0.8, 2.0),
                   None if fcf is None else (100.0 if fcf > 0 else 0.0)])
    if de is not None and de > 200:
        neg.append(f"High debt: D/E {de / 100:.1f}x")
    if fcf is not None and fcf < 0:
        neg.append("Negative free cash flow")

    # --- momentum ---
    momentum = None
    if trend and trend.get("mode") == "full":
        momentum = _avg([100.0 if trend["above_200dma"] else 0.0,
                         100.0 if trend["golden_cross"] else 0.0,
                         _scale(trend.get("momentum_30d_pct"), -10, 10)])
        (pos if trend["above_200dma"] else neg).append(
            f"Price {'above' if trend['above_200dma'] else 'below'} its 200-day average")
    elif p.get("return_3m") is not None or p.get("return_1y") is not None:
        momentum = _avg([_scale(p.get("return_3m"), -10, 15), _scale(p.get("return_1y"), -20, 40)])
        r1y = p.get("return_1y")
        if r1y is not None:
            (pos if r1y >= 15 else neg if r1y < -10 else []).append(f"1-year price return {r1y:+.0f}%")
    elif trend:
        momentum = _scale(trend.get("momentum_pct"), -10, 10)

    scores = {"value": value, "profitability": profitability, "income": income,
              "growth": growth, "health": health, "momentum": momentum}
    score, coverage = _weighted(scores, WEIGHTS)
    rating = _rating(score, coverage)
    if rating in ("Strong Buy", "Buy") and momentum is not None and momentum < 30:
        rating = _downgrade(rating)
        neg.append("Weak price trend — rating lowered one notch until it turns")
    if rating in ("Strong Buy", "Buy") and eps is not None and eps <= 0:
        rating = "Hold"
    if rating == "Strong Buy" and coverage < 0.85:
        # e.g. NSE: the free source has no margins/ROE/debt data
        rating = "Buy"
        neg.append("Limited data (no profitability/debt figures) — capped at Buy")

    return {
        "ticker": p["ticker"], "name": p.get("name"), "market": p.get("market"),
        "type": "stock", "sector": p.get("sector"), "industry": p.get("industry"),
        "currency": p.get("currency"), "price": p.get("price"), "price_usd": p.get("price_usd"),
        "market_cap": p.get("market_cap"), "market_cap_usd": p.get("market_cap_usd"),
        "enterprise_value": p.get("enterprise_value"),
        "metrics": {k: p.get(k) for k in (
            "pe_ratio", "forward_pe", "pb_ratio", "ps_ratio", "ev_to_ebitda", "peg_ratio",
            "eps", "earnings_yield_pct", "book_value_per_share",
            "roe_pct", "roa_pct", "profit_margin_pct", "operating_margin_pct", "gross_margin_pct",
            "revenue", "net_income", "free_cash_flow",
            "revenue_growth_pct", "earnings_growth_pct",
            "dividend_yield", "dividend_per_share", "payout_ratio", "dividend_growth_years",
            "debt_to_equity", "current_ratio", "beta", "week52_high", "week52_low",
            "analyst_target", "analyst_upside_pct", "analyst_rating", "analyst_count",
            "return_1w", "return_3m", "return_6m", "return_1y", "return_ytd", "day_change_pct",
        )},
        "trend": trend,
        "scores": scores, "score": score, "coverage": coverage,
        "recommendation": rating, "positives": pos, "negatives": neg,
        "source": p.get("source"),
    }


# ---------- ETFs ----------

def analyze_etf(profile: dict, trend: dict | None) -> dict:
    p = profile
    pos, neg = [], []
    er, dy, aum = p.get("expense_ratio_pct"), p.get("dividend_yield"), p.get("total_assets")
    r3, r5 = p.get("return_3y_avg"), p.get("return_5y_avg")
    momentum = None
    if trend and trend.get("mode") == "full":
        momentum = _avg([100.0 if trend["above_200dma"] else 0.0,
                         _scale(trend.get("momentum_30d_pct"), -8, 8)])
        (pos if trend["above_200dma"] else neg).append(
            f"Price {'above' if trend['above_200dma'] else 'below'} its 200-day average")
    scores = {
        "cost": _scale(er, 0.75, 0.03),
        "returns": _avg([_scale(r3, 0, 15), _scale(r5, 0, 15)]),
        "income": _scale(dy, 0, 5) if dy else None,
        "size": None if not aum else _scale(aum, 1e8, 5e10),
        "momentum": momentum,
    }
    if er is not None:
        (pos if er <= 0.2 else neg if er > 0.5 else []).append(f"Expense ratio {er:.2f}%")
    for label, r in (("3-yr", r3), ("5-yr", r5)):
        if r is not None:
            (pos if r >= 8 else neg if r < 0 else []).append(f"{label} avg return {r:.1f}%/yr")
    score, coverage = _weighted(scores, ETF_WEIGHTS)
    rating = _rating(score, coverage)
    if rating in ("Strong Buy", "Buy") and momentum is not None and momentum < 30:
        rating = _downgrade(rating)
        neg.append("Weak price trend — rating lowered one notch until it turns")
    return {
        "ticker": p["ticker"], "name": p.get("name"), "market": p.get("market"), "type": "etf",
        "currency": p.get("currency"), "price": p.get("price"), "price_usd": p.get("price_usd"),
        "metrics": {"expense_ratio_pct": er, "dividend_yield": dy, "total_assets": aum,
                    "return_ytd": p.get("return_ytd"), "return_3y_avg": r3, "return_5y_avg": r5,
                    "week52_high": p.get("week52_high"), "week52_low": p.get("week52_low"),
                    "return_1y": p.get("return_1y"), "return_3m": p.get("return_3m")},
        "trend": trend, "scores": scores, "score": score, "coverage": coverage,
        "recommendation": rating, "positives": pos, "negatives": neg, "source": p.get("source"),
    }


# ---------- universe ----------

def analyze_universe(screener, watchlist: dict) -> dict:
    """Analyse every configured ticker. Uses screener.fundamentals (cached
    profiles) and screener.trend_context so nothing is fetched twice."""
    stocks, etfs, failed = [], [], []
    entries = ([(t, "us") for t in watchlist["us_stocks"]] +
               [(t, "nse") for t in watchlist["nse_kenya"]] +
               [(t, "us", "etf") for t in watchlist["etfs"]])
    for entry in entries:
        ticker, market = entry[0], entry[1]
        try:
            profile = screener.fundamentals.get_profile(ticker, market=market)
            if not profile:
                failed.append(ticker)
                continue
            trend = screener.trend_context(ticker, market=market)
            if len(entry) == 3 or profile.get("quote_type") == "ETF":
                etfs.append(analyze_etf(profile, trend))
            else:
                stocks.append(analyze_stock(profile, trend))
        except Exception as e:
            logger.warning(f"Analysis failed for {ticker}: {e}")
            failed.append(ticker)

    key = lambda r: (r["score"] is not None, r["score"] or 0)
    stocks.sort(key=key, reverse=True)
    etfs.sort(key=key, reverse=True)
    return {
        "stocks": stocks,
        "etfs": etfs,
        "failed": failed,
        "kes_per_usd": screener.fundamentals.kes_per_usd(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def top_picks(analysis: dict, market: str, n: int = 5) -> list:
    return [s for s in analysis.get("stocks", [])
            if s["market"] == market and s["recommendation"] in ("Strong Buy", "Buy")][:n]
