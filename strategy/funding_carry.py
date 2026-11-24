"""
Funding-rate carry (cash-and-carry basis): decision rules and backtest.

The trade: buy spot and short the same notional of the perpetual future. Price
exposure nets to ~zero, and while the funding rate is positive the short leg
RECEIVES funding every interval (8h on Binance/OKX). The edge is that funding,
not a price view.

This module is pure logic (no network, no ccxt) so the same functions can drive
the backtest and any later live wiring.

What the backtest models:
  * funding received / paid every interval, signed (negative funding costs you)
  * entry/exit costs on all four legs (buy spot, short perp, sell spot, cover
    perp) as taker fee + slippage
  * capital deployed = spot notional + perp margin (notional / leverage), so
    returns are on the money actually tied up, not on notional
  * decisions use only funding observed BEFORE the interval being collected
    (no lookahead)

What it does NOT model; read before trusting a number:
  * basis P&L (spot-vs-perp price gap moving while you hold). Usually small
    versus funding but real, and it is what liquidates a thin short leg
  * exchange failure / withdrawal freezes (the dominant tail risk)
  * margin liquidation of the short leg in a violent rally: keep leverage low
  * borrowing costs if you use margin spot instead of owning the coin
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 24 * 365


@dataclass
class CarryConfig:
    entry_apr_pct: float = 12.0       # enter when smoothed APR, net of amortised costs, >= this
    exit_apr_pct: float = 3.0         # exit when smoothed APR falls below this
    lookback_intervals: int = 9       # smoothing window (9 x 8h = 3 days)
    interval_hours: float = 8.0
    taker_fee_bps: float = 10.0       # per leg; use your real tier
    slippage_bps: float = 3.0         # per leg
    leverage: float = 2.0             # on the perp short only; margin = notional / leverage
    expected_hold_days: float = 14.0  # round-trip cost is amortised over this when gating entry
    min_hold_intervals: int = 3       # don't flip-flop on one noisy print


def annualize_funding(rate_per_interval: float, interval_hours: float = 8.0) -> float:
    """Per-interval funding rate (0.0001 = 0.01%) -> simple annual rate in percent."""
    return rate_per_interval * (HOURS_PER_YEAR / interval_hours) * 100.0


def round_trip_cost_pct(cfg: CarryConfig) -> float:
    """Cost of opening AND closing the pair, in % of notional (4 legs)."""
    return 4 * (cfg.taker_fee_bps + cfg.slippage_bps) / 100.0


def net_apr_after_costs(smoothed_apr_pct: float, cfg: CarryConfig) -> float:
    """Smoothed funding APR minus round-trip cost amortised over the expected hold."""
    amortised = round_trip_cost_pct(cfg) * (365.0 / cfg.expected_hold_days)
    return smoothed_apr_pct - amortised


def should_enter(smoothed_apr_pct: float, cfg: CarryConfig) -> bool:
    return net_apr_after_costs(smoothed_apr_pct, cfg) >= cfg.entry_apr_pct


def should_exit(smoothed_apr_pct: float, cfg: CarryConfig) -> bool:
    return smoothed_apr_pct < cfg.exit_apr_pct


def backtest_carry(funding: pd.Series, cfg: CarryConfig = None,
                   initial_balance: float = 1000.0) -> dict:
    """Backtest on a series of per-interval funding rates (index = settlement time).

    Interval i's funding is collected only if the position was opened on the
    basis of rates up to i-1.
    """
    cfg = cfg or CarryConfig()
    rates = funding.dropna().astype(float)
    n = len(rates)
    if n <= cfg.lookback_intervals + 1:
        return _summarise(np.array([initial_balance]), [], initial_balance, 0, 0.0, 0.0, 0.0)

    smoothed_apr = rates.rolling(cfg.lookback_intervals).mean() \
        * (HOURS_PER_YEAR / cfg.interval_hours) * 100.0

    deploy_ratio = 1.0 + 1.0 / cfg.leverage     # spot notional + perp margin, per 1 of notional
    leg_cost = (cfg.taker_fee_bps + cfg.slippage_bps) / 10_000.0

    balance = initial_balance
    equity = np.full(n, initial_balance, dtype=float)
    in_pos = False
    held = 0
    notional = 0.0
    trades = []
    funding_collected = 0.0
    costs_paid = 0.0
    intervals_in_market = 0
    entry_idx = 0
    entry_balance = 0.0

    for i in range(cfg.lookback_intervals, n):
        signal_apr = smoothed_apr.iloc[i - 1]   # strictly prior information

        if in_pos:
            pnl = rates.iloc[i] * notional      # short perp receives when rate > 0
            balance += pnl
            funding_collected += pnl
            held += 1
            intervals_in_market += 1
            if held >= cfg.min_hold_intervals and should_exit(signal_apr, cfg):
                cost = 2 * leg_cost * notional  # sell spot + cover perp
                balance -= cost
                costs_paid += cost
                trades.append({"entry": rates.index[entry_idx], "exit": rates.index[i],
                               "intervals": held, "net_pnl": balance - entry_balance})
                in_pos, held, notional = False, 0, 0.0
        elif not np.isnan(signal_apr) and should_enter(signal_apr, cfg):
            notional = balance / deploy_ratio
            cost = 2 * leg_cost * notional      # buy spot + short perp
            entry_balance = balance
            balance -= cost
            costs_paid += cost
            entry_idx = i
            in_pos, held = True, 0

        equity[i] = balance

    if in_pos:  # close at the end so open trades are not hidden
        cost = 2 * leg_cost * notional
        balance -= cost
        costs_paid += cost
        equity[-1] = balance
        trades.append({"entry": rates.index[entry_idx], "exit": rates.index[-1],
                       "intervals": held, "net_pnl": balance - entry_balance,
                       "open_at_end": True})

    years = n * cfg.interval_hours / HOURS_PER_YEAR
    return _summarise(equity, trades, initial_balance, years, funding_collected,
                      costs_paid, intervals_in_market / n)


def _summarise(equity, trades, initial, years, funding_collected, costs_paid, time_in_market):
    eq = pd.Series(equity)
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min() * 100)
    final = float(eq.iloc[-1])
    total_ret = (final / initial - 1) * 100
    apr = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0.0
    return {
        "final_balance": round(final, 2),
        "total_return_pct": round(total_ret, 2),
        "annualized_return_pct": round(apr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": len(trades),
        "funding_collected": round(funding_collected, 2),
        "costs_paid": round(costs_paid, 2),
        "time_in_market_pct": round(time_in_market * 100, 1),
        "equity": eq,
        "trade_log": trades,
    }
