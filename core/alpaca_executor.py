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
            "qty": str(qty) if qty == int(qty) else str(qty),
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
        resp.raise_for_status()
        return resp.json()

    def _get_order(self, order_id: str) -> dict:
        url = f"{self.base_url}/v2/orders/{order_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def open_trade(self, symbol: str, side: str, proposed_amount: float,
                    entry_price: float, stop_loss_pct: float, take_profit_pct: float):
        """
        proposed_amount is in USD (notional). Alpaca supports fractional shares.
        qty = notional / price for market buy.
        """
        decision = self.risk.pre_trade_check(proposed_amount)
        if not decision.allowed:
            self.notifier.notify("trade_rejected", f"{symbol} {side} rejected: {decision.reason}")
            return None

        # Calculate quantity from notional
        if entry_price > 0:
            qty = decision.position_size / entry_price
        else:
            return None

        if qty <= 0:
            return None

        client_order_id = self._new_client_order_id(symbol)

        if self.dry_run:
            fill_price = entry_price
            filled_qty = qty
            order_id = client_order_id
        else:
            order_side = "buy" if side in ("buy",) else "sell"
            result = self._submit_order(
                symbol, qty, order_side,
                order_type="market",
                time_in_force="day",
            )
            order_id = result.get("id", client_order_id)
            # Alpaca market orders fill almost instantly — poll for fill
            import time
            for _ in range(10):
                time.sleep(0.5)
                filled = self._get_order(order_id)
                if filled.get("status") == "filled":
                    fill_price = float(filled.get("filled_avg_price", entry_price))
                    filled_qty = float(filled.get("filled_qty", qty))
                    break
            else:
                fill_price = entry_price
                filled_qty = qty

            # Place stop-loss order (Alpaca supports stop orders natively)
            sl_price = entry_price * (1 - stop_loss_pct / 100) if side == "buy" \
                else entry_price * (1 + stop_loss_pct / 100)
            sl_side = "sell" if side == "buy" else "buy"
            try:
                self._submit_order(
                    symbol, filled_qty, sl_side,
                    order_type="stop",
                    stop_price=sl_price,
                    time_in_force="gtc",
                )
            except Exception:
                self.notifier.notify(
                    "warning",
                    f"Stop-loss order failed for {symbol} — position unprotected on exchange. "
                    f"Bot-side trailing stop is active as backup.",
                    priority="high",
                )

        sl_price = entry_price * (1 - stop_loss_pct / 100) if side == "buy" \
            else entry_price * (1 + stop_loss_pct / 100)
        tp_price = entry_price * (1 + take_profit_pct / 100) if side == "buy" \
            else entry_price * (1 - take_profit_pct / 100)

        self.state.record_trade_open(
            order_id, "alpaca", symbol, side, filled_qty,
            fill_price, sl_price, tp_price,
        )
        self.notifier.notify(
            "trade_opened",
            f"Alpaca {side.upper()} {filled_qty:.4f} {symbol} @ {fill_price:.2f} "
            f"(SL: {sl_price:.2f}, TP: {tp_price:.2f}) "
            f"[{'DRY-RUN' if self.dry_run else 'LIVE'}]",
        )
        return order_id

    def close_trade(self, client_order_id: str, exit_price: float):
        positions = {p["client_order_id"]: p for p in self.state.get_open_positions()}
        pos = positions.get(client_order_id)
        if not pos:
            return

        if not self.dry_run:
            close_side = "sell" if pos["side"] == "buy" else "buy"
            try:
                self._submit_order(
                    pos["symbol"], pos["amount"], close_side,
                    order_type="market",
                    time_in_force="day",
                )
            except Exception as e:
                self.notifier.notify(
                    "warning",
                    f"Failed to close {pos['symbol']} on Alpaca: {e}",
                    priority="high",
                )
                return

            # Cancel any remaining stop orders for this symbol
            try:
                url = f"{self.base_url}/v2/orders"
                resp = requests.get(url, headers=self._headers(),
                                   params={"status": "open", "symbols": pos["symbol"]},
                                   timeout=10)
                open_orders = resp.json().get("orders", [])
                for order in open_orders:
                    cancel_url = f"{self.base_url}/v2/orders/{order['id']}"
                    requests.delete(cancel_url, headers=self._headers(), timeout=5)
            except Exception:
                pass

        direction = 1 if pos["side"] == "buy" else -1
        pnl = direction * (exit_price - pos["entry_price"]) * pos["amount"]

        self.state.record_trade_close(client_order_id, exit_price, pnl)
        self.risk.on_trade_closed(pnl)
