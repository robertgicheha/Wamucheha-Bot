"""
The screener's filter pills. Each returns ranked symbols from rows that carry the data
it needs; a row without that figure is left out, never ranked as zero.
"""

LARGE_CAP = {"nse": 10e9, "us": 10e9}      # in the market's own currency
SMALL_CAP = {"nse": 2e9, "us": 2e9}
PENNY = {"nse": 5.0, "us": 5.0}
NEAR_HIGH, NEAR_LOW = 0.98, 1.02           # "at" a high or low means within 2%


def _fund(key):
    return lambda r: (r.get("fund") or {}).get(key)


def _metric(key):
    return lambda r: (r.get("metrics") or {}).get(key)


def _year(r):
    return r["metrics"]["perf"].get("1y")


def _yield(r):
    return (r.get("dividend") or {}).get("yield_pct")


def _traded(r):
    return r["price"] * r["volume"] if r.get("volume") else None


def _near(level_key, source, high: bool):
    """How close the price is to a high or low, as price / level; None if not near it."""
    def value(r):
        level = (r.get(source) or {}).get(level_key)
        if not level:
            return None
        ratio = r["price"] / level
        return ratio if (ratio >= NEAR_HIGH if high else ratio <= NEAR_LOW) else None
    return value


def definitions(market: str) -> list:
    """(key, label, value getter, keep test, highest first, what it needs, description)."""
    money = "KES" if market == "nse" else "USD"
    pos = lambda v: v > 0
    some = lambda v: True
    return [
        ("all", "All stocks", _fund("market_cap"), None, True, "", "Everything tracked, largest first where market value is known."),
        ("top_gainers", "Top gainers", lambda r: r.get("change_pct"), pos, True, "", "Biggest rises today."),
        ("biggest_losers", "Biggest losers", lambda r: r.get("change_pct"), lambda v: v < 0, False, "", "Biggest falls today."),
        ("large_cap", "Large-cap", _fund("market_cap"), lambda v: v >= LARGE_CAP[market], True, "market value", f"Market value of {money} {LARGE_CAP[market] / 1e9:g} billion or more."),
        ("small_cap", "Small-cap", _fund("market_cap"), lambda v: v < SMALL_CAP[market], False, "market value", f"Market value under {money} {SMALL_CAP[market] / 1e9:g} billion."),
        ("largest_employers", "Largest employers", _fund("employees"), pos, True, "employee counts", "Companies with the most employees."),
        ("high_dividend", "High-dividend", _yield, pos, True, "dividend figures", "Highest dividend yield. A very high yield can mean the price has fallen or a cut is coming."),
        ("net_income", "Highest net income", _fund("net_income"), pos, True, "net income", "Largest net profit."),
        ("highest_cash", "Highest cash", _fund("cash"), pos, True, "cash figures", "Most cash on hand."),
        ("profit_per_employee", "Highest profit per employee", _fund("profit_per_employee"), pos, True, "net income and employee counts", "Net income divided by employees."),
        ("revenue_per_employee", "Highest revenue per employee", _fund("revenue_per_employee"), pos, True, "revenue and employee counts", "Revenue divided by employees."),
        ("most_active", "Most active", _traded, pos, True, "volume", "Most money traded today (price times volume)."),
        ("unusual_volume", "Unusual volume", _metric("rel_vol"), lambda v: v >= 1.5, True, "11 days of volume history", "Volume at least 1.5 times the 10-day average."),
        ("most_volatile", "Most volatile", _metric("volatility_1m"), pos, True, "a month of price history", "Largest price swings over the last month, annualised."),
        ("high_beta", "High beta", _fund("beta"), lambda v: v > 1, True, "beta", "Moves more than the market (beta above 1)."),
        ("best_performing", "Best performing", _year, some, True, "a year of price history", "Price change over 12 months."),
        ("highest_revenue", "Highest revenue", _fund("revenue"), pos, True, "revenue", "Largest annual revenue."),
        ("most_expensive", "Most expensive", lambda r: r.get("price"), pos, True, "", "Highest price per share."),
        ("penny", "Penny stocks", lambda r: r.get("price"), lambda v: v < PENNY[market], False, "", f"Shares priced under {money} {PENNY[market]:g}. Often thinly traded and very risky."),
        ("overbought", "Overbought", _metric("rsi"), lambda v: v > 70, True, "30 days of price history", "RSI above 70."),
        ("oversold", "Oversold", _metric("rsi"), lambda v: v < 30, False, "30 days of price history", "RSI below 30."),
        ("ath", "All-time high", _near("all_time_high", "fund", True), some, True, "full price history", "Within 2% of the highest price on record (US history here starts in 1970)."),
        ("atl", "All-time low", _near("all_time_low", "fund", False), some, False, "full price history", "Within 2% of the lowest price on record (US history here starts in 1970)."),
        ("high_52w", "52-week high", _near("high_52w", "metrics", True), some, True, "a year of price history", "Within 2% of the 52-week high."),
        ("low_52w", "52-week low", _near("low_52w", "metrics", False), some, False, "a year of price history", "Within 2% of the 52-week low."),
    ]


ETF_LABELS = {"all": "All ETFs", "large_cap": "Large funds"}


def run(rows: list, market: str) -> list:
    """[{"key", "label", "needs", "description", "symbols"}] in the order the pills are shown."""
    out = []
    for key, label, value, keep, high_first, needs, description in definitions(market):
        if key == "all":
            ranked = sorted(rows, key=lambda r: (-(value(r) or (r.get("fund") or {}).get("aum") or 0),
                                                 -(_traded(r) or 0), r["symbol"]))
        else:
            found = [(value(r), r) for r in rows]
            found = [(v, r) for v, r in found if v is not None and keep(v)]
            found.sort(key=lambda x: (-x[0] if high_first else x[0], x[1]["symbol"]))
            ranked = [r for _, r in found]
        out.append({"key": key, "label": label, "needs": needs, "description": description,
                    "symbols": [r["symbol"] for r in ranked]})
    return out
