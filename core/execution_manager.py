"""
Execution manager — enhanced with trade metadata, slippage tracking, and portfolio risk.

Passes strategy information, scores, and regime data through to the
notification system for rich trade logging. Tracks slippage between
signal price and actual fill price.
"""
import uuid
import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from core.structured_logger import slippage_tracker, strategy_perf_tracker, api_failure_tracker, log_trade_open, log_trade_close


class ExecutionManager:
    def __init__(self, exchange_id: str, api_key: str, api_secret: str,
                 state_manager, risk_manager, notifier, dry_run: bool = True):
        self.state = state_manager
        self.risk = risk_manager
        self.notifier = notifier
        self.dry_run = dry_run
        exchange_cls = getattr(ccxt, exchange_id)
        self.exchange = exchange_cls({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        })
        self.exchange_id = exchange_id
        # client_order_id -> exchange order id of its resting stop-loss order.
        # In-memory only: after a restart the bot's own position monitor still
        # enforces the stop, and close_trade() sells whatever is actually free.
        self._stop_orders = {}

    def _new_client_order_id(self, symbol: str) -> str:
        # Alphanumeric only: OKX rejects '-' in clOrdId (max 32 chars).
        return f"bot{symbol.replace('/', '')}{uuid.uuid4().hex[:12]}"

    def _base_amount(self, symbol: str, notional_usd: float, price: float) -> float:
        """Convert a USD notional into the base-asset quantity the exchange expects."""
        if price <= 0:
            return 0.0
        if self.dry_run:
            return notional_usd / price
        self.exchange.load_markets()
        return float(self.exchange.amount_to_precision(symbol, notional_usd / price))

    def _min_order_problem(self, symbol: str, amount: float, price: float):
        """Return a reason string if the order is below the exchange minimums, else None."""
        if self.dry_run:
            return None
        limits = self.exchange.market(symbol)["limits"]
        min_amount = limits["amount"]["min"] or 0
        min_cost = limits["cost"]["min"] or 0
        if amount <= 0 or amount < min_amount:
            return f"amount {amount} below exchange minimum {min_amount}"
        if amount * price < min_cost:
            return f"order value ${amount * price:.2f} below exchange minimum ${min_cost}"
        return None

    def _cancel_stop_order(self, client_order_id: str, symbol: str):
        stop_id = self._stop_orders.pop(client_order_id, None)
        if not stop_id:
            return
        try:
            params = {"stop": True} if self.exchange_id == "okx" else {}
            self.exchange.cancel_order(stop_id, symbol, params)
        except Exception as e:
            # Already triggered/cancelled — nothing left to cancel.
            api_failure_tracker.record_failure(self.exchange_id, e, f"cancel_stop_{symbol}")

    def _free_base(self, symbol: str, amount: float) -> float:
        """Free base-asset balance capped at amount (fees paid in the base coin shrink holdings)."""
        base = self.exchange.market(symbol)["base"]
        free = float((self.exchange.fetch_balance().get(base) or {}).get("free") or 0)
        return min(amount, free)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ccxt.NetworkError, ccxt.ExchangeNotAvailable)),
        reraise=True,
    )
    def _submit_order(self, symbol, side, amount, order_type, params):
        return self.exchange.create_order(symbol, order_type, side, amount, params=params)

    def open_trade(self, symbol: str, side: str, proposed_amount: float,
                    entry_price: float, stop_loss_pct: float, take_profit_pct: float,
                    strategies: list = None, score: float = 0, regime: str = ""):
        # Spot markets can't open shorts: a sell needs coins we don't hold.
        if side != "buy":
            return None

        # Portfolio-level risk check (correlation, asset-class caps, max positions)
        decision = self.risk.pre_trade_check(proposed_amount, symbol=symbol)
        if not decision.allowed:
            self.notifier.notify("trade_rejected", f"{symbol} {side} rejected: {decision.reason}")
            return None

        client_order_id = self._new_client_order_id(symbol)

        if self.state.is_duplicate_order(client_order_id):
            return None

        stop_price = entry_price * (1 - stop_loss_pct / 100) if side == "buy" \
            else entry_price * (1 + stop_loss_pct / 100)
        target_price = entry_price * (1 + take_profit_pct / 100) if side == "buy" \
            else entry_price * (1 - take_profit_pct / 100)

        # decision.position_size is a USD notional; exchanges want base-asset quantity.
        try:
            amount = self._base_amount(symbol, decision.position_size, entry_price)
            problem = self._min_order_problem(symbol, amount, entry_price)
        except Exception as e:
            api_failure_tracker.record_failure(self.exchange_id, e, f"size_{symbol}")
            self.notifier.notify("trade_rejected", f"{symbol} sizing failed: {e}")
            return None
        if problem:
            self.notifier.notify("trade_rejected", f"{symbol} {side} rejected: {problem}")
            return None

        if self.dry_run:
            fill_price = entry_price
            filled_amount = amount
        else:
            try:
                order = self._submit_order(
                    symbol, side, amount, "market",
                    {"clientOrderId": client_order_id},
                )
                fill_price = order.get("average") or order.get("price") or entry_price
                filled_amount = order.get("filled") or amount
            except Exception as e:
                api_failure_tracker.record_failure(self.exchange_id, e, f"open_trade_{symbol}")
                self.notifier.notify("trade_rejected", f"{symbol} order failed: {e}")
                return None

            try:
                # Spot stop-loss: Binance STOP_LOSS / OKX conditional order, market on trigger.
                sl_amount = float(self.exchange.amount_to_precision(symbol, self._free_base(symbol, filled_amount)))
                sl_order = self._submit_order(
                    symbol, "sell", sl_amount, "market", {"stopLossPrice": stop_price},
                )
                self._stop_orders[client_order_id] = sl_order["id"]
            except Exception as e:
                api_failure_tracker.record_failure(self.exchange_id, e, f"stop_loss_{symbol}")
                self.notifier.notify("trade_rejected",
                    f"{symbol} stop-loss order failed ({e}) — bot-side stop still active")

        self.state.record_trade_open(
            client_order_id, self.exchange_id, symbol, side, filled_amount,
            fill_price, stop_price, target_price,
            strategies=strategies, score=score, regime=regime,
        )

        # Track slippage (signal price vs actual fill)
        slippage_tracker.record(
            symbol=symbol, side=side,
            signal_price=entry_price, fill_price=fill_price,
            exchange=self.exchange_id,
        )

        # Structured log
        log_trade_open(symbol, side, filled_amount, fill_price,
                       self.exchange_id, strategies, score)

        # Rich notification
        self.notifier.notify_trade_opened(
            symbol=symbol, side=side, amount=filled_amount,
            entry_price=fill_price, stop_loss=stop_price,
            take_profit=target_price, exchange=self.exchange_id,
            dry_run=self.dry_run, strategies=strategies,
            score=score, regime=regime,
        )

        return client_order_id

    def close_trade(self, client_order_id: str, exit_price: float, reason: str = ""):
        positions = {p["client_order_id"]: p for p in self.state.get_open_positions()}
        pos = positions.get(client_order_id)
        if not pos:
            return

        if not self.dry_run:
            symbol = pos["symbol"]
            try:
                # Free the coins locked in the resting stop-loss before selling them.
                self._cancel_stop_order(client_order_id, symbol)
                sell_amount = float(self.exchange.amount_to_precision(
                    symbol, self._free_base(symbol, pos["amount"])))
                min_amount = self.exchange.market(symbol)["limits"]["amount"]["min"] or 0
                if sell_amount > 0 and sell_amount >= min_amount:
                    self._submit_order(symbol, "sell", sell_amount, "market",
                                       {"clientOrderId": f"{client_order_id}CL"})
                else:
                    # Coins already gone (exchange stop-loss fired, or sold manually).
                    self.notifier.notify("position_drift",
                        f"{symbol}: nothing left to sell — exchange stop-loss likely fired; recording close")
            except Exception as e:
                # Leave the position open so the monitor retries next tick
                # instead of recording a close that never happened.
                api_failure_tracker.record_failure(self.exchange_id, e, f"close_trade_{symbol}")
                self.notifier.notify("trade_rejected", f"{symbol} close failed: {e}", priority="high")
                return

        direction = 1 if pos["side"] == "buy" else -1
        pnl = direction * (exit_price - pos["entry_price"]) * pos["amount"]

        # Get trade metadata for rich notification
        from sqlalchemy.orm import Session
        from core.state_manager import TradeRow, engine
        strategies = None
        score = None
        regime = None
        with Session(engine) as session:
            trade = session.query(TradeRow).filter_by(
                client_order_id=client_order_id
            ).first()
            if trade:
                strategies = trade.strategies
                score = trade.score
                regime = trade.regime
                if strategies:
                    import json
                    strategies = json.loads(strategies)

        self.state.record_trade_close(client_order_id, exit_price, pnl, reason)
        self.risk.on_trade_closed(pnl)

        # Structured logging
        log_trade_close(pos["symbol"], pos["side"], pos["entry_price"],
                        exit_price, pnl, reason, self.exchange_id)

        # Track per-strategy performance
        if strategies:
            strategy_perf_tracker.record_trade(strategies, pnl, pos["symbol"])

        # Rich notification
        self.notifier.notify_trade_closed(
            symbol=pos["symbol"], side=pos["side"], amount=pos["amount"],
            entry_price=pos["entry_price"], exit_price=exit_price,
            pnl=pnl, exchange=self.exchange_id, reason=reason,
            strategies=strategies,
        )
