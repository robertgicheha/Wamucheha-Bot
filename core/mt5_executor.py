"""
MetaTrader 5 (MT5) execution manager.

Connects to a locally-running MT5 terminal via the MetaTrader5 Python package.
Supports all major forex pairs, gold (XAUUSD), crypto (BTCUSD), and CFDs.

Key differences from ccxt/OANDA:
- MT5 uses a symbol-based model (XAUUSD, EURUSD, etc.)
- Volume is in lots, not units or notional
- Stop-loss/take-profit are attached at order time
- Requires the MetaTrader5 terminal to be running on the same machine
"""
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger("mt5_executor")


class MT5Executor:
    def __init__(self, state_manager, risk_manager, notifier,
                 login: int = 0, password: str = "", server: str = "",
                 dry_run: bool = True):
        self.state = state_manager
        self.risk = risk_manager
        self.notifier = notifier
        self.dry_run = dry_run
        self.login = login
        self.password = password
        self.server = server
        self._connected = False

    def connect(self) -> bool:
        """Initialize connection to the MT5 terminal."""
        try:
            import MetaTrader5 as mt5
        except ImportError:
            logger.error("MetaTrader5 package not installed. Run: pip install MetaTrader5")
            return False

        if not mt5.initialize():
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        if self.login:
            authorized = mt5.login(self.login, password=self.password, server=self.server)
            if not authorized:
                logger.error(f"MT5 login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False

        self._connected = True
        info = mt5.account_info()
        if info:
            logger.info(f"MT5 connected: {info.login} @ {info.server}, "
                       f"Balance: {info.balance}, Leverage: 1:{info.leverage}")
        return True

    def shutdown(self):
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except Exception:
            pass
        self._connected = False

    def _new_client_order_id(self, symbol: str) -> str:
        return f"bot-mt5-{symbol}-{uuid.uuid4().hex[:12]}"

    def _get_lot_size(self, symbol: str, notional_usd: float, price: float) -> float:
        """Convert USD notional to lot size for the given symbol."""
        try:
            import MetaTrader5 as mt5
            info = mt5.symbol_info(symbol)
            if info is None:
                return 0.0
            lot_step = info.volume_step
            min_lot = info.volume_min
            max_lot = info.volume_max

            # For forex: 1 lot = 100,000 units. For gold: 1 lot = 100 oz.
            # Volume in lots = notional_usd / (lot_size_in_units * price)
            # Simplified: lots = notional_usd / (contract_size * price)
            contract_size = info.trade_contract_size
            if contract_size and price > 0:
                lots = notional_usd / (contract_size * price)
            else:
                lots = notional_usd / (price * 100000) if price > 0 else 0

            # Round to lot step
            lots = max(min_lot, min(max_lot, lots))
            lots = round(lots / lot_step) * lot_step
            return round(lots, 2)
        except Exception as e:
            logger.error(f"Failed to calculate lot size: {e}")
            return 0.0

    def open_trade(self, symbol: str, side: str, proposed_amount: float,
                    entry_price: float, stop_loss_pct: float, take_profit_pct: float,
                    strategies: list = None, score: float = 0, regime: str = ""):
        decision = self.risk.pre_trade_check(proposed_amount)
        if not decision.allowed:
            self.notifier.notify("trade_rejected", f"MT5 {symbol} {side} rejected: {decision.reason}")
            return None

        client_order_id = self._new_client_order_id(symbol)

        if side in ("buy",):
            sl_price = entry_price * (1 - stop_loss_pct / 100)
            tp_price = entry_price * (1 + take_profit_pct / 100)
            order_type = "buy"
        else:
            sl_price = entry_price * (1 + stop_loss_pct / 100)
            tp_price = entry_price * (1 - take_profit_pct / 100)
            order_type = "sell"

        if self.dry_run:
            fill_price = entry_price
            lots = self._get_lot_size(symbol, decision.position_size, entry_price) or 0.01
        else:
            try:
                import MetaTrader5 as mt5
                if not self._connected:
                    self.connect()

                lots = self._get_lot_size(symbol, decision.position_size, entry_price)
                if lots <= 0:
                    return None

                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": lots,
                    "type": mt5.ORDER_TYPE_BUY if order_type == "buy" else mt5.ORDER_TYPE_SELL,
                    "price": mt5.symbol_info(symbol).ask if order_type == "buy" else mt5.symbol_info(symbol).bid,
                    "sl": round(sl_price, mt5.symbol_info(symbol).digits),
                    "tp": round(tp_price, mt5.symbol_info(symbol).digits),
                    "deviation": 20,
                    "magic": 202401,
                    "comment": client_order_id,
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                result = mt5.order_send(request)
                if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                    err = result.comment if result else "No result"
                    logger.error(f"MT5 order failed: {err}")
                    self.notifier.notify("trade_rejected", f"MT5 order failed: {err}")
                    return None
                fill_price = result.price
            except Exception as e:
                logger.error(f"MT5 order error: {e}")
                self.notifier.notify("trade_rejected", f"MT5 error: {e}")
                return None

        self.state.record_trade_open(
            client_order_id, "mt5", symbol, side, lots,
            fill_price, sl_price, tp_price,
            strategies=strategies, score=score, regime=regime,
        )
        self.notifier.notify_trade_opened(
            symbol=symbol, side=side, amount=lots,
            entry_price=fill_price, stop_loss=sl_price,
            take_profit=tp_price, exchange="mt5",
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
            try:
                import MetaTrader5 as mt5
                close_type = mt5.ORDER_TYPE_SELL if pos["side"] == "buy" else mt5.ORDER_TYPE_BUY
                price = mt5.symbol_info(pos["symbol"]).bid if pos["side"] == "buy" \
                    else mt5.symbol_info(pos["symbol"]).ask

                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": pos["symbol"],
                    "volume": pos["amount"],
                    "type": close_type,
                    "position": self._find_position_ticket(pos["symbol"]),
                    "price": price,
                    "deviation": 20,
                    "magic": 202401,
                    "comment": f"close-{client_order_id}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                result = mt5.order_send(request)
                if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                    logger.error(f"MT5 close failed: {result.comment if result else 'No result'}")
            except Exception as e:
                logger.error(f"MT5 close error: {e}")

        direction = 1 if pos["side"] == "buy" else -1
        pnl = direction * (exit_price - pos["entry_price"]) * pos["amount"]

        # Get trade metadata
        from sqlalchemy.orm import Session
        from core.state_manager import TradeRow, engine
        strategies = None
        with Session(engine) as session:
            trade = session.query(TradeRow).filter_by(client_order_id=client_order_id).first()
            if trade and trade.strategies:
                import json
                strategies = json.loads(trade.strategies)

        self.state.record_trade_close(client_order_id, exit_price, pnl, reason)
        self.risk.on_trade_closed(pnl)

        self.notifier.notify_trade_closed(
            symbol=pos["symbol"], side=pos["side"], amount=pos["amount"],
            entry_price=pos["entry_price"], exit_price=exit_price,
            pnl=pnl, exchange="mt5", reason=reason,
            strategies=strategies,
        )

    def _find_position_ticket(self, symbol: str) -> int:
        """Find the ticket number for an open position on this symbol."""
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(symbol=symbol)
            if positions and len(positions) > 0:
                return positions[0].ticket
        except Exception as e:
            logger.error(f"Failed to find position ticket: {e}")
        return 0
