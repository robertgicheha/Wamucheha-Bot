"""
Alpaca Markets execution manager for US Stocks, ETFs, and Commodities (via ETFs).

Alpaca is commission-free and has an excellent API with built-in paper trading.
Key differences from crypto exchange execution:
- Stocks are discrete units (you can't buy 0.001 shares with most brokers, but
  Alpaca supports fractional shares for market orders)
- Trading hours: 9:30 AM - 4:00 PM ET (no 24/7 like crypto)
- Pre-market: 4:00 AM - 9:30 AM ET, After-hours: 4:00 PM - 8:00 PM ET
- Stop-loss and stop-limit orders are supported natively

IMPORTANT: Alpaca paper trading uses the same API endpoints but different keys
(PK- prefix for paper, AK- prefix for live). This is the primary safety net —
you can test with real market data and fake money.
"""
import math
import time
import uuid
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


class AlpacaExecutor:
    def __init__(self, api_key: str, api_secret: str, state_manager, risk_manager,
                 notifier, paper: bool = True, dry_run: bool = True):
        self.state = state_manager
        self.risk = risk_manager
        self.notifier = notifier
        self.dry_run = dry_run
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = ("https://paper-api.alpaca.markets" if paper
                         else "https://api.alpaca.markets")
        self._clock = (0.0, True)  # (checked_at, is_open)

    def is_market_open(self) -> bool:
        """Broker clock (handles holidays/half-days), cached for a minute."""
        checked_at, is_open = self._clock
        if time.time() - checked_at < 60:
            return is_open
        try:
            resp = requests.get(f"{self.base_url}/v2/clock", headers=self._headers(), timeout=10)
            resp.raise_for_status()
            is_open = bool(resp.json().get("is_open"))
        except Exception:
            is_open = False  # can't confirm -> don't trade
        self._clock = (time.time(), is_open)
        return is_open

    def _cancel_open_orders(self, symbol: str):
        """Cancel resting orders (the stop-loss) — they hold the shares, so a
        close order for the same qty is rejected while they exist."""
        resp = requests.get(f"{self.base_url}/v2/orders", headers=self._headers(),
                            params={"status": "open", "symbols": symbol}, timeout=10)
        resp.raise_for_status()
        for order in resp.json():  # this endpoint returns a list
            requests.delete(f"{self.base_url}/v2/orders/{order['id']}",
                            headers=self._headers(), timeout=10)

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
        }

    def _new_client_order_id(self, symbol: str) -> str:
        return f"bot-alpaca-{symbol}-{uuid.uuid4().hex[:12]}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True,
    )
    def _submit_order(self, symbol: str, qty: float, side: str,
                      order_type: str = "market", **kwargs) -> dict:
        body = {
            "symbol": symbol,
            "qty": f"{qty:.6f}".rstrip("0").rstrip("."),  # Alpaca allows <= 9 decimals
            "side": side,
            "type": order_type,
            "time_in_force": kwargs.get("time_in_force", "day"),
        }
        if "limit_price" in kwargs:
            body["limit_price"] = str(kwargs["limit_price"])
        if "stop_price" in kwargs:
            body["stop_price"] = str(kwargs["stop_price"])
        if "trail_percent" in kwargs:
            body["trail_percent"] = str(kwargs["trail_percent"])

        url = f"{self.base_url}/v2/orders"
        resp = requests.post(url, headers=self._headers(), json=body, timeout=15)
        if resp.status_code in (403, 422):
            # Rejected by Alpaca (not retryable) — surface its reason.
            raise ValueError(f"Alpaca rejected order: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()

    def _get_order(self, order_id: str) -> dict:
        url = f"{self.base_url}/v2/orders/{order_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def open_trade(self, symbol: str, side: str, proposed_amount: float,
                    entry_price: float, stop_loss_pct: float, take_profit_pct: float,
                    strategies: list = None, score: float = 0, regime: str = ""):
        """
        proposed_amount is in USD (notional). Alpaca supports fractional shares.
        qty = notional / price for market buy.
        """
        decision = self.risk.pre_trade_check(proposed_amount, symbol=symbol, venue="alpaca")
        if not decision.allowed:
            self.notifier.notify("trade_rejected", f"{symbol} {side} rejected: {decision.reason}")
            return None

        # Calculate quantity from notional
        if entry_price > 0:
            qty = round(decision.position_size / entry_price, 6)
        else:
            return None

        # Alpaca: fractional orders need >= $1 notional; shorts can't be
        # fractional, so a short is rounded down to whole shares.
        if side != "buy":
            qty = math.floor(qty)
        if qty <= 0 or qty * entry_price < 1:
            self.notifier.notify("trade_rejected",
                f"{symbol} {side} skipped: ${decision.position_size:.2f} is below Alpaca's minimum "
                f"({'1 whole share for shorts' if side != 'buy' else '$1 fractional'})")
            return None

        client_order_id = self._new_client_order_id(symbol)

        if self.dry_run:
            fill_price = entry_price
            filled_qty = qty
            order_id = client_order_id
        else:
            order_side = "buy" if side in ("buy",) else "sell"
            try:
                result = self._submit_order(
                    symbol, qty, order_side,
                    order_type="market",
                    time_in_force="day",
                )
            except Exception as e:
                self.notifier.notify("trade_rejected", f"Alpaca {symbol} order failed: {e}")
                return None
            order_id = result.get("id", client_order_id)
            for _ in range(10):
                time.sleep(0.5)
                filled = self._get_order(order_id)
                if filled.get("status") == "filled":
                    fill_price = float(filled.get("filled_avg_price", entry_price))
                    filled_qty = float(filled.get("filled_qty", qty))
                    break
            else:
                # Not filled in 5s (halted / queued) — cancel rather than record
                # a position that may never exist.
                try:
                    requests.delete(f"{self.base_url}/v2/orders/{order_id}",
                                    headers=self._headers(), timeout=10)
                except Exception:
                    pass
                self.notifier.notify("trade_rejected", f"Alpaca {symbol} order not filled in 5s — cancelled")
                return None

            sl_price = entry_price * (1 - stop_loss_pct / 100) if side == "buy" \
                else entry_price * (1 + stop_loss_pct / 100)
            sl_side = "sell" if side == "buy" else "buy"
            try:
                # Fractional-share stop orders must be DAY orders on Alpaca;
                # the bot-side stop in position_monitor covers overnight.
                self._submit_order(
                    symbol, filled_qty, sl_side,
                    order_type="stop",
                    stop_price=round(sl_price, 2),
                    time_in_force="gtc" if filled_qty == int(filled_qty) else "day",
                )
            except Exception as e:
                self.notifier.notify(
                    "warning",
                    f"Stop-loss order failed for {symbol} ({e}) — position unprotected on exchange. "
                    f"Bot-side stop is active as backup.",
                    priority="high",
                )

        sl_price = entry_price * (1 - stop_loss_pct / 100) if side == "buy" \
            else entry_price * (1 + stop_loss_pct / 100)
        tp_price = entry_price * (1 + take_profit_pct / 100) if side == "buy" \
            else entry_price * (1 - take_profit_pct / 100)

        self.state.record_trade_open(
            order_id, "alpaca", symbol, side, filled_qty,
            fill_price, sl_price, tp_price,
            strategies=strategies, score=score, regime=regime,
        )
        self.notifier.notify_trade_opened(
            symbol=symbol, side=side, amount=filled_qty,
            entry_price=fill_price, stop_loss=sl_price,
            take_profit=tp_price, exchange="alpaca",
            dry_run=self.dry_run, strategies=strategies,
            score=score, regime=regime,
        )
        return order_id

    def close_trade(self, client_order_id: str, exit_price: float, reason: str = ""):
        positions = {p["client_order_id"]: p for p in self.state.get_open_positions()}
        pos = positions.get(client_order_id)
        if not pos:
            return

        if not self.dry_run:
            try:
                self._cancel_open_orders(pos["symbol"])
                # Closes via the position endpoint (handles fractional + short cover).
                resp = requests.delete(f"{self.base_url}/v2/positions/{pos['symbol']}",
                                       headers=self._headers(),
                                       params={"qty": f"{pos['amount']:.6f}".rstrip("0").rstrip(".")},
                                       timeout=15)
                if resp.status_code == 404:
                    # No position at the broker: the stop-loss already filled.
                    self.notifier.notify("position_drift",
                        f"Alpaca {pos['symbol']}: position already closed at broker (stop-loss filled?)")
                elif resp.status_code >= 400:
                    raise ValueError(resp.text[:200])
            except Exception as e:
                # Leave the position open so the monitor retries next tick.
                self.notifier.notify(
                    "warning",
                    f"Failed to close {pos['symbol']} on Alpaca: {e}",
                    priority="high",
                )
                return

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
            pnl=pnl, exchange="alpaca", reason=reason,
            strategies=strategies,
        )
