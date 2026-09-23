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

from core.mt5_client import get_mt5

logger = logging.getLogger("mt5_executor")


class MT5Executor:
    def __init__(self, state_manager, risk_manager, notifier,
                 login: int = 0, password: str = "", server: str = "",
                 dry_run: bool = True, allow_min_lot: bool = False):
        self.state = state_manager
        # MT5's smallest order (0.01 lot) is ~$1,000 of EURUSD / ~$4,000 of gold.
        # When the risk-sized position is smaller, skip the trade unless the
        # oversized minimum lot is explicitly allowed (e.g. on a demo account).
        self.allow_min_lot = allow_min_lot
        self._min_lot_alerted = {}  # symbol -> date, so the 15s loop alerts once a day
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
            mt5 = get_mt5()
            initialized = mt5.initialize()
        except ImportError:
            logger.error("MetaTrader5 package not installed (Windows only). "
                         "On Linux set MT5_RPC_HOST to the mt5 container.")
            return False
        except Exception as e:
            logger.error(f"MT5 bridge unreachable: {e}")
            return False

        if not initialized:
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
            mt5 = get_mt5()
            mt5.shutdown()
        except Exception:
            pass
        self._connected = False

    def _new_client_order_id(self, symbol: str) -> str:
        return f"bot-mt5-{symbol}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _filling_mode(mt5, info) -> int:
        """Pick a filling mode the broker supports for this symbol (bit 1 = FOK, bit 2 = IOC)."""
        if info.filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _get_lot_size(self, symbol: str, notional_usd: float, price: float) -> float:
        """Convert USD notional to lot size for the given symbol."""
        try:
            mt5 = get_mt5()
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

            if lots < min_lot:
                unit_value = (contract_size or 100000) * price
                if not self.allow_min_lot:
                    today = datetime.now(timezone.utc).date()
                    if self._min_lot_alerted.get(symbol) == today:
                        return 0.0
                    self._min_lot_alerted[symbol] = today
                    self.notifier.notify("trade_rejected",
                        f"MT5 {symbol} skipped: position ${notional_usd:.2f} is below the minimum "
                        f"{min_lot} lot (~${min_lot * unit_value:,.0f}). Raise the trading balance "
                        f"or set execution.mt5_allow_min_lot: true (demo accounts).")
                    return 0.0
                logger.warning(f"MT5 {symbol}: using minimum lot {min_lot} "
                               f"(~${min_lot * unit_value:,.0f}) for a ${notional_usd:.2f} position")

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
        decision = self.risk.pre_trade_check(proposed_amount, symbol=symbol)
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
            lots = self._get_lot_size(symbol, decision.position_size, entry_price)
            if lots <= 0:
                return None
        else:
            try:
                mt5 = get_mt5()
                if not self._connected:
                    self.connect()

                mt5.symbol_select(symbol, True)  # must be in Market Watch to trade/quote
                lots = self._get_lot_size(symbol, decision.position_size, entry_price)
                if lots <= 0:
                    return None

                info = mt5.symbol_info(symbol)
                tick = mt5.symbol_info_tick(symbol)
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": lots,
                    "type": mt5.ORDER_TYPE_BUY if order_type == "buy" else mt5.ORDER_TYPE_SELL,
                    "price": tick.ask if order_type == "buy" else tick.bid,
                    "sl": round(sl_price, info.digits),
                    "tp": round(tp_price, info.digits),
                    "deviation": 20,
                    "magic": 202401,
                    "comment": client_order_id[:31],
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": self._filling_mode(mt5, info),
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
                mt5 = get_mt5()
                ticket = self._find_position_ticket(pos["symbol"], client_order_id)
                if ticket:
                    close_type = mt5.ORDER_TYPE_SELL if pos["side"] == "buy" else mt5.ORDER_TYPE_BUY
                    info = mt5.symbol_info(pos["symbol"])
                    tick = mt5.symbol_info_tick(pos["symbol"])

                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": pos["symbol"],
                        "volume": pos["amount"],
                        "type": close_type,
                        "position": ticket,
                        "price": tick.bid if pos["side"] == "buy" else tick.ask,
                        "deviation": 20,
                        "magic": 202401,
                        "comment": f"close-{client_order_id}"[:31],
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": self._filling_mode(mt5, info),
                    }
                    result = mt5.order_send(request)
                    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                        raise RuntimeError(result.comment if result else f"No result {mt5.last_error()}")
                else:
                    # Broker already closed it (its SL/TP fired) — just record the close.
                    logger.info(f"MT5 {pos['symbol']} position already closed at broker")
            except Exception as e:
                # Leave the position open so the monitor retries next tick.
                logger.error(f"MT5 close error: {e}")
                self.notifier.notify("trade_rejected", f"MT5 {pos['symbol']} close failed: {e}",
                                     priority="high")
                return

        direction = 1 if pos["side"] == "buy" else -1
        pnl = direction * (exit_price - pos["entry_price"]) * pos["amount"]
        try:
            # amount is in lots; let MT5 apply contract size + currency conversion.
            mt5 = get_mt5()
            open_type = mt5.ORDER_TYPE_BUY if pos["side"] == "buy" else mt5.ORDER_TYPE_SELL
            calc = mt5.order_calc_profit(open_type, pos["symbol"], pos["amount"],
                                         pos["entry_price"], exit_price)
            if calc is not None:
                pnl = calc
        except Exception as e:
            logger.warning(f"MT5 order_calc_profit failed, using raw estimate: {e}")

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

    def _find_position_ticket(self, symbol: str, client_order_id: str = "") -> int:
        """Find the ticket of the position this bot order opened (matched by comment)."""
        mt5 = get_mt5()
        positions = mt5.positions_get(symbol=symbol) or ()
        for p in positions:
            if client_order_id and p.comment == client_order_id[:31]:
                return p.ticket
        # Fallback: brokers may rewrite comments — take our own (magic) position.
        for p in positions:
            if p.magic == 202401:
                return p.ticket
        return 0
