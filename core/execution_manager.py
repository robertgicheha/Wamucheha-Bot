"""
Execution manager — enhanced with trade metadata tracking.

Passes strategy information, scores, and regime data through to the
notification system for rich trade logging.
"""
import uuid
import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


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

    def _new_client_order_id(self, symbol: str) -> str:
        return f"bot-{symbol.replace('/', '')}-{uuid.uuid4().hex[:12]}"

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
        decision = self.risk.pre_trade_check(proposed_amount)
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

        if self.dry_run:
            fill_price = entry_price
            filled_amount = decision.position_size
        else:
            order = self._submit_order(
                symbol, side, decision.position_size, "market",
                {"clientOrderId": client_order_id},
            )
            fill_price = order.get("average") or order.get("price") or entry_price
            filled_amount = order.get("filled", decision.position_size)

            stop_side = "sell" if side == "buy" else "buy"
            self._submit_order(
                symbol, stop_side, filled_amount, "stop_market",
                {"stopPrice": stop_price, "clientOrderId": f"{client_order_id}-SL", "reduceOnly": True},
            )

        self.state.record_trade_open(
            client_order_id, self.exchange_id, symbol, side, filled_amount,
            fill_price, stop_price, target_price,
            strategies=strategies, score=score, regime=regime,
        )

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
            close_side = "sell" if pos["side"] == "buy" else "buy"
            self._submit_order(
                pos["symbol"], close_side, pos["amount"], "market",
                {"clientOrderId": f"{client_order_id}-CLOSE", "reduceOnly": True},
            )

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

        # Rich notification
        self.notifier.notify_trade_closed(
            symbol=pos["symbol"], side=pos["side"], amount=pos["amount"],
            entry_price=pos["entry_price"], exit_price=exit_price,
            pnl=pnl, exchange=self.exchange_id, reason=reason,
            strategies=strategies,
        )
