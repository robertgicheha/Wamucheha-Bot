"""
Event-driven backtester for the strategy ensemble in strategy/technical_strategy.py.

Routes every entry/exit decision through the exact same functions live trading
uses (strategy.decision.evaluate() for entries, technical_strategy.generate_exit_signal()
for proactive exits, mirroring core/position_monitor.py's check order) so a
backtest result describes the strategy that actually runs live, not a
simplified stand-in for it.

Deliberately models:
- Trading fees (both entry and exit)
- Slippage (execution price worse than signal price), side-aware
- Short positions (side-aware stop/target/PnL — a long-only backtester
  silently mis-scores every short trade)
- The same layered exit priority as core/position_monitor.py:
    1. Fixed stop-loss / take-profit (the floor, checked via that bar's
       high/low so an intrabar touch isn't missed)
    2. Proactive exit signal (trend reversal / RSI exhaustion / trailing
       stop / channel breaks) via generate_exit_signal()
- Walk-forward split: fit/eyeball on the "train" period, but the number you
  should actually trust is the "test" period performance — the strategy
  hasn't seen it.

This does NOT model partial fills, order book depth, or funding rates (for
perpetuals) — for a real capital decision, those all matter more as size grows.
Treat any backtest, including this one, as a lower bound on how wrong you could be,
not a promise of live performance.

Note: Portfolio-level strategies (arbitrage, rotation, DCA, safe-haven, options)
are NOT backtested here — they require multi-symbol data and real-time price feeds.
"""
import pandas as pd
from strategy.technical_strategy import compute_indicators, generate_exit_signal
from strategy.signal_aggregator import SignalAggregator
from strategy import decision


def run_backtest(df: pd.DataFrame, initial_balance: float = 1000.0,
                  risk_fraction: float = 0.02, stop_loss_pct: float = 1.5,
                  take_profit_pct: float = 3.0, fee_pct: float = 0.1,
                  slippage_pct: float = 0.05, cfg: dict = None,
                  use_aggregator: bool = True,
                  min_aggregator_confidence: float = 0.3,
                  trailing_activate_pct: float = 1.5,
                  trailing_distance_pct: float = 1.0) -> dict:
    cfg = cfg or {}
    aggregator = SignalAggregator(cfg) if use_aggregator else None

    # Precompute indicators once over the whole series (indicators are
    # causal/backward-looking only — using data up to bar i to score bar i
    # is identical whether computed on the full df or a growing window, and
    # computing it once instead of once per bar avoids O(n^2) backtests).
    df = compute_indicators(df, cfg)

    balance = initial_balance
    equity_curve = [balance]
    trades = []
    position = None
    peak_price = None
    bars_in_position = 0

    for i in range(25, len(df)):
        window = df.iloc[:i + 1]
        last = window.iloc[-1]

        if position is None:
            signal = decision.evaluate(
                window, risk_fraction, balance, cfg=cfg,
                aggregator=aggregator,
                min_aggregator_confidence=min_aggregator_confidence,
                _df_has_indicators=True,
            )
            if signal:
                side = signal["side"]
                raw_entry = signal["entry_price"]
                # Buys slip up (you pay more), sells/shorts slip down (you receive less)
                entry_price = raw_entry * (1 + slippage_pct / 100) if side == "buy" \
                    else raw_entry * (1 - slippage_pct / 100)
                amount_usd = signal["amount"]
                entry_fee = amount_usd * fee_pct / 100
                if side == "buy":
                    stop = entry_price * (1 - stop_loss_pct / 100)
                    target = entry_price * (1 + take_profit_pct / 100)
                else:
                    stop = entry_price * (1 + stop_loss_pct / 100)
                    target = entry_price * (1 - take_profit_pct / 100)
                position = {
                    "side": side,
                    "entry_price": entry_price,
                    "amount_usd": amount_usd - entry_fee,
                    "stop_loss_price": stop,
                    "take_profit_price": target,
                }
                balance -= entry_fee
                peak_price = entry_price
                bars_in_position = 0
        else:
            bars_in_position += 1
            side = position["side"]
            peak_price = max(peak_price, last["high"]) if side == "buy" else min(peak_price, last["low"])

            exit_price = None
            exit_reason = None

            # 1. Fixed stop-loss / take-profit (the floor) — checked via
            # intrabar high/low, same order position_monitor.py uses.
            if side == "buy":
                if last["low"] <= position["stop_loss_price"]:
                    exit_price, exit_reason = position["stop_loss_price"], "stop_loss_hit"
                elif last["high"] >= position["take_profit_price"]:
                    exit_price, exit_reason = position["take_profit_price"], "take_profit_hit"
            else:
                if last["high"] >= position["stop_loss_price"]:
                    exit_price, exit_reason = position["stop_loss_price"], "stop_loss_hit"
                elif last["low"] <= position["take_profit_price"]:
                    exit_price, exit_reason = position["take_profit_price"], "take_profit_hit"

            # 2. Proactive exit signal (trend reversal / RSI exhaustion / trailing stop)
            if exit_price is None:
                pos_with_peak = {**position, "peak_price": peak_price, "bars_held": bars_in_position}
                exit_signal = generate_exit_signal(
                    window, pos_with_peak,
                    trailing_activate_pct=trailing_activate_pct,
                    trailing_distance_pct=trailing_distance_pct,
                    cfg=cfg, _df_has_indicators=True,
                )
                if exit_signal:
                    exit_price, exit_reason = exit_signal["exit_price"], exit_signal["reason"]

            if exit_price is not None:
                slipped_exit = exit_price * (1 - slippage_pct / 100) if side == "buy" \
                    else exit_price * (1 + slippage_pct / 100)
                pct_change = (slipped_exit - position["entry_price"]) / position["entry_price"] \
                    if side == "buy" else (position["entry_price"] - slipped_exit) / position["entry_price"]
                gross_pnl = position["amount_usd"] * pct_change
                exit_fee = position["amount_usd"] * fee_pct / 100
                net_pnl = gross_pnl - exit_fee
                balance += net_pnl
                trades.append({
                    "side": side,
                    "pnl": net_pnl,
                    "win": net_pnl > 0,
                    "exit_reason": exit_reason,
                    "bars_held": bars_in_position,
                })
                position = None
                peak_price = None

        equity_curve.append(balance)

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    max_dd = _max_drawdown(equity_curve)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0

    return {
        "final_balance": balance,
        "total_return_pct": (balance - initial_balance) / initial_balance * 100,
        "num_trades": len(trades),
        "num_long": len([t for t in trades if t["side"] == "buy"]),
        "num_short": len([t for t in trades if t["side"] == "sell"]),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0,
        "max_consecutive_losses": _max_consecutive_losses(trades),
        "max_drawdown_pct": max_dd,
        "avg_win": avg_win,
        "avg_loss": -avg_loss,
        "reward_risk_ratio": (avg_win / avg_loss) if avg_loss else float("inf") if avg_win else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf") if gross_win else 0,
        "expectancy_per_trade": (sum(t["pnl"] for t in trades) / len(trades)) if trades else 0,
        "avg_bars_held": (sum(t["bars_held"] for t in trades) / len(trades)) if trades else 0,
    }


def _max_drawdown(equity_curve) -> float:
    peak = equity_curve[0]
    max_dd = 0
    for val in equity_curve:
        peak = max(peak, val)
        dd = (peak - val) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd


def _max_consecutive_losses(trades) -> int:
    streak = max_streak = 0
    for t in trades:
        if not t["win"]:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def walk_forward_backtest(df: pd.DataFrame, train_frac: float = 0.6, **kwargs) -> dict:
    """Split chronologically — never shuffle time-series data. Report both halves;
    if 'test' performance is dramatically worse than 'train', the strategy is
    likely overfit to the train period's specific noise."""
    split = int(len(df) * train_frac)
    train_df, test_df = df.iloc[:split], df.iloc[split:]
    return {
        "train": run_backtest(train_df, **kwargs),
        "test": run_backtest(test_df, **kwargs),
    }
