"""
Simple event-driven backtester for the strategies in strategy/technical_strategy.py.

Deliberately models:
- Trading fees (both entry and exit)
- Slippage (execution price worse than signal price)
- Walk-forward split: fit/eyeball on the "train" period, but the number you should
  actually trust is the "test" period performance — the strategy hasn't seen it.

This does NOT model partial fills, order book depth, or funding rates (for
perpetuals) — for a real capital decision, those all matter more as size grows.
Treat any backtest, including this one, as a lower bound on how wrong you could be,
not a promise of live performance.
"""
import pandas as pd
from strategy.technical_strategy import generate_signal


def run_backtest(df: pd.DataFrame, initial_balance: float = 1000.0,
                  risk_fraction: float = 0.02, stop_loss_pct: float = 1.5,
                  take_profit_pct: float = 3.0, fee_pct: float = 0.1,
                  slippage_pct: float = 0.05) -> dict:
    balance = initial_balance
    equity_curve = [balance]
    trades = []
    position = None

    for i in range(25, len(df)):
        window = df.iloc[:i + 1]
        last = window.iloc[-1]

        if position is None:
            signal = generate_signal(window, risk_fraction, balance)
            if signal:
                entry_price = signal["entry_price"] * (1 + slippage_pct / 100)  # buy slips up
                amount_usd = signal["amount"]
                entry_fee = amount_usd * fee_pct / 100
                position = {
                    "entry_price": entry_price,
                    "amount_usd": amount_usd - entry_fee,
                    "stop": entry_price * (1 - stop_loss_pct / 100),
                    "target": entry_price * (1 + take_profit_pct / 100),
                }
                balance -= entry_fee
        else:
            hit_stop = last["low"] <= position["stop"]
            hit_target = last["high"] >= position["target"]
            if hit_stop or hit_target:
                exit_price = position["stop"] if hit_stop else position["target"]
                exit_price *= (1 - slippage_pct / 100)  # sell slips down
                pct_change = (exit_price - position["entry_price"]) / position["entry_price"]
                gross_pnl = position["amount_usd"] * pct_change
                exit_fee = position["amount_usd"] * fee_pct / 100
                net_pnl = gross_pnl - exit_fee
                balance += net_pnl
                trades.append({"pnl": net_pnl, "win": net_pnl > 0, "exit_reason": "stop" if hit_stop else "target"})
                position = None

        equity_curve.append(balance)

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    max_dd = _max_drawdown(equity_curve)

    return {
        "final_balance": balance,
        "total_return_pct": (balance - initial_balance) / initial_balance * 100,
        "num_trades": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0,
        "max_consecutive_losses": _max_consecutive_losses(trades),
        "max_drawdown_pct": max_dd,
        "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
        "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
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
