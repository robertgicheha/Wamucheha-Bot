"""
Sokowatch-Web's rule-based screener and market insights (src/lib/ai/intelligence.ts),
ported to Python. Same sector weights, points and BUY / HOLD / AVOID cut-offs.
A match score says how well a stock fits a risk profile. It is not a forecast.
"""
import math

RISKS = ("conservative", "balanced", "aggressive")
SECTOR_GROUPS = ("Banking", "Insurance", "Telecommunications", "Energy", "Manufacturing", "Agriculture")
_GROUP_ALIASES = {"Utilities": "Energy", "Agricultural": "Agriculture", "Telecommunication": "Telecommunications",
                  "Manufacturing & Allied": "Manufacturing", "Construction & Allied": "Manufacturing"}

SECTOR_WEIGHTS = {
    "conservative": {"Banking": .35, "Insurance": .2, "Telecommunications": .2, "Energy": .1,
                     "Manufacturing": .1, "Agriculture": .03, "Other": .02},
    "balanced": {"Banking": .25, "Insurance": .15, "Telecommunications": .2, "Energy": .1,
                 "Manufacturing": .15, "Agriculture": .08, "Other": .07},
    "aggressive": {"Banking": .15, "Insurance": .1, "Telecommunications": .15, "Energy": .15,
                   "Manufacturing": .2, "Agriculture": .12, "Other": .13},
}
STOP_LOSS_PCT = {"conservative": .08, "balanced": .12, "aggressive": .15}
_LIQUIDITY_PTS = {"conservative": 25, "balanced": 20, "aggressive": 15}
_MOMENTUM_PTS = {"conservative": 5, "balanced": 10, "aggressive": 15}


def group_of(sector: str) -> str:
    sector = _GROUP_ALIASES.get(sector, sector)
    return sector if sector in SECTOR_GROUPS else "Other"


def _clamp(v, lo, hi):
    return min(hi, max(lo, v))


def format_volume(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(int(v))


def score_stock(stock: dict, risk: str, preferred=()) -> dict:
    group = group_of(stock["sector"])
    weights = SECTOR_WEIGHTS[risk]
    weight = weights.get(group, 0)
    chosen = group in preferred
    sector_pts = weight / max(weights.values()) * 40 + (10 if chosen else 0)

    liquidity = _clamp((math.log10(max(stock["volume"], 1)) - 4) / 3, 0, 1)
    momentum = _clamp(stock["change_pct"] / 5, -1, 1)
    raw = 20 + sector_pts + liquidity * _LIQUIDITY_PTS[risk] + momentum * _MOMENTUM_PTS[risk]
    score = int(_clamp(math.floor(raw + .5), 0, 100))     # JS Math.round, not banker's rounding

    if chosen:
        reasons = [f"{group} is one of the sectors you selected"]
    else:
        size = "a core sector" if weight >= .2 else "a smaller part"
        reasons = [f"{group} is {size} ({round(weight * 100)}%) of a {risk} allocation"]
    if liquidity >= .66:
        reasons.append(f"Actively traded ({format_volume(stock['volume'])} shares today), so easier to buy and sell")
    elif liquidity < .33:
        reasons.append(f"Thinly traded ({format_volume(stock['volume'])} shares today); exits may be slow or costly")
    if abs(stock["change_pct"]) >= .01:
        reasons.append(f"{'Up' if stock['change_pct'] > 0 else 'Down'} {abs(stock['change_pct']):.2f}% on the day")

    return {"match_score": score,
            "verdict": "BUY" if score >= 70 else "HOLD" if score >= 50 else "AVOID",
            "stop_loss": round(stock["price"] * (1 - STOP_LOSS_PCT[risk]), 2),
            "match_reasons": reasons}


def market_insights(stocks: list, market: str, currency: str) -> dict:
    priced = [s for s in stocks if s.get("price") and s.get("change_pct") is not None]
    if not priced:
        return {"summary": "Market data is unavailable right now.", "points": []}
    up = sum(s["change_pct"] > 0 for s in priced)
    down = sum(s["change_pct"] < 0 for s in priced)
    by_change = sorted(priced, key=lambda s: s["change_pct"], reverse=True)
    busiest = max(priced, key=lambda s: s["volume"])
    points = [{"title": "Market breadth", "impact": "positive" if up > down else "negative" if down > up else "neutral",
               "text": f"{up} up, {down} down and {len(priced) - up - down} unchanged out of {len(priced)} tracked."}]
    top, bottom = by_change[0], by_change[-1]
    if top["change_pct"] > 0:
        points.append({"title": "Biggest gainer", "impact": "positive",
                       "text": f"{top['symbol']} ({top['name']}) is up {top['change_pct']:.2f}% at {currency} {top['price']:,.2f}."})
    if bottom["change_pct"] < 0:
        points.append({"title": "Biggest decliner", "impact": "negative",
                       "text": f"{bottom['symbol']} ({bottom['name']}) is down {abs(bottom['change_pct']):.2f}% at {currency} {bottom['price']:,.2f}."})
    points.append({"title": "Most active by volume", "impact": "neutral",
                   "text": f"{busiest['symbol']} leads with {format_volume(busiest['volume'])} shares traded."})
    moves: dict = {}
    for s in priced:
        moves.setdefault(s["sector"], []).append(s["change_pct"])
    ranked = sorted(((sum(m) / len(m), name) for name, m in moves.items() if len(m) >= 2 and name not in ("Other", "ETF", "US stock")), reverse=True)  # last two are not sectors
    if len(ranked) >= 2:
        points.append({"title": "Sector moves", "impact": "neutral",
                       "text": f"Strongest: {ranked[0][1]} (average {ranked[0][0]:+.2f}%). "
                               f"Weakest: {ranked[-1][1]} ({ranked[-1][0]:+.2f}%)."})
    return {"summary": f"{up} of {len(priced)} {market} securities are advancing versus {down} declining in the latest prices.",
            "points": points}
