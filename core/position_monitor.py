"""
Position monitor — the missing piece that actually SELLS.

Before this module: execution_manager.open_trade() attached a stop-loss and
(in dry-run) computed a take-profit price, but nothing ever watched price
against those levels and nothing ever called execution_manager.close_trade().
In live mode the exchange-side stop order would eventually catch a loss, but
there was no proactive exit, no take-profit execution, and in dry-run mode
positions never closed at all.

This module is called once per main loop tick, for every open position:
  1. Pull latest price/candles for the symbol.
  2. Check the fixed TP/SL levels (belt).
  3. Check technical_strategy.generate_exit_signal for a proactive exit
     — trend reversal, RSI exhaustion, or trailing stop (suspenders).
  4. If either fires, call executor.close_trade(), which records PnL through
     state_manager and risk_manager (updates balance, consecutive losses,
     triggers profit sweep / circuit breakers as usual).

Live-mode note: in live trading the exchange-side stop-loss order is the
authoritative floor — it protects you even if this process crashes. This
monitor is what lets you exit BETTER than the floor, and is what makes
dry-run mode behave like a real backtest instead of positions that never close.
"""
import logging
from strategy.technical_strategy import generate_exit_signal

logger = logging.getLogger("position_monitor")

# In-memory peak-price tracking for trailing stops, keyed by client_order_id.
# Resets on process restart — acceptable: worst case a trailing stop briefly
# re-arms from the current price instead of the true prior peak. The hard
# fixed take-profit/stop-loss still bound the trade regardless.
_peak_prices: dict[str, float] = {}


def check_and_close_positions(state_manager, executors: dict, feed_router,
                               nse_exchange_names=("nse",),
                               trailing_activate_pct: float = 1.5,
                               trailing_distance_pct: float = 1.0,
                               strategy_cfg: dict = None):
    """Call once per main loop tick. Mutates state via executor.close_trade()."""
    positions = state_manager.get_open_positions()

    for pos in positions:
        exchange_name = pos["exchange"]
        symbol = pos["symbol"]
        client_order_id = pos["client_order_id"]

        if exchange_name in nse_exchange_names:
            continue  # NSE is alert-only, no execution/positions expected here

        executor = executors.get(exchange_name)
        if executor is None:
            continue

        try:
            df = feed_router.get_ohlcv(symbol, timeframe="15m", limit=200)
        except Exception as e:
            logger.warning(f"Position monitor: failed to fetch {symbol}: {e}")
            continue

        if df is None or len(df) < 50:
            continue

        last_price = float(df.iloc[-1]["close"])
        _peak_prices[client_order_id] = max(_peak_prices.get(client_order_id, pos["entry_price"]), last_price)
        pos_with_peak = {**pos, "peak_price": _peak_prices[client_order_id]}

        # 1. Fixed stop-loss / take-profit (the floor)
        if pos["side"] == "buy":
            if pos["stop_loss_price"] and last_price <= pos["stop_loss_price"]:
                _do_close(executor, state_manager, client_order_id, last_price, "stop_loss_hit")
                continue
            if pos["take_profit_price"] and last_price >= pos["take_profit_price"]:
                _do_close(executor, state_manager, client_order_id, last_price, "take_profit_hit")
                continue
        else:  # short (OANDA/Alpaca can go short; Binance spot here does not)
            if pos["stop_loss_price"] and last_price >= pos["stop_loss_price"]:
                _do_close(executor, state_manager, client_order_id, last_price, "stop_loss_hit")
                continue
            if pos["take_profit_price"] and last_price <= pos["take_profit_price"]:
                _do_close(executor, state_manager, client_order_id, last_price, "take_profit_hit")
                continue

        # 2. Proactive exit signal (trend reversal / RSI exhaustion / trailing stop)
        exit_signal = generate_exit_signal(
            df, pos_with_peak,
            trailing_activate_pct=trailing_activate_pct,
            trailing_distance_pct=trailing_distance_pct,
            cfg=strategy_cfg,
        )
        if exit_signal:
            _do_close(executor, state_manager, client_order_id, exit_signal["exit_price"], exit_signal["reason"])


def _do_close(executor, state_manager, client_order_id, exit_price, reason):
    logger.info(f"Closing {client_order_id} @ {exit_price} — reason: {reason}")
    executor.close_trade(client_order_id, exit_price)
    _peak_prices.pop(client_order_id, None)