"""
OANDA v20 REST API execution manager for Forex and Commodities.

OANDA uses a different order model than crypto exchanges:
- Units-based (not notional/amount in USD)
- Stop-loss and take-profit are part of the order request (OCO-like)
- No client order IDs — OANDA uses its own order IDs
- Fractional units supported (e.g. 100.5 units of EUR/USD)

This mirrors the ExecutionManager interface so main.py can call the same
open_trade/close_trade methods regardless of the underlying broker.
"""
import uuid
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


class OandaExecutor:
    def __init__(self, api_key: str, account_id: str, state_manager, risk_manager,
                 notifier, practice: bool = False, dry_run: bool = True):
        self.state = state_manager
        self.risk = risk_manager
        self.notifier = notifier
        self.dry_run = dry_run
        self.api_key = api_key
        self.account_id = account_id
        self.base_url = ("https://api-fxpractice.oanda.com" if practice
                         else "https://api-fxtrade.oanda.com")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _normalize_instrument(self, symbol: str) -> str:
        return symbol.replace("/", "_")

    def _new_client_order_id(self, symbol: str) -> str:
        return f"bot-oanda-{symbol.replace('/', '')}-{uuid.uuid4().hex[:12]}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True,
    )
    def _submit_order(self, instrument: str, units: int, stop_loss: float = None,
                      take_profit: float = None) -> dict:
        order_body = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",  # Fill or Kill — no partial fills lingering
        }

        if stop_loss:
            order_body["stopLossOnFill"] = {
                "price": f"{stop_loss:.5f}",
                "timeInForce": "GTC",
            }
        if take_profit:
            order_body["takeProfitOnFill"] = {
                "price": f"{take_profit:.5f}",
            }

        url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
        resp = requests.post(url, headers=self._headers(),
                            json={"order": order_body}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def open_trade(self, symbol: str, side: str, proposed_amount: float,
                    entry_price: float, stop_loss_pct: float, take_profit_pct: float):
        """
        proposed_amount is in USD (notional). OANDA needs units, so we convert:
        units = floor(notional_value / current_price)
        For forex, 1 unit of EUR/USD ≈ 1.10 USD (the quote price).
        """
        decision = self.risk.pre_trade_check(proposed_amount)
        if not decision.allowed:
            self.notifier.notify("trade_rejected", f"{symbol} {side} rejected: {decision.reason}")
            return None

        instrument = self._normalize_instrument(symbol)

        # Convert USD notional to units
        if entry_price > 0:
            units = int(decision.position_size / entry_price)
        else:
            return None

        if units == 0:
            return None

        # For sell/short, units must be negative
        if side in ("sell", "short"):
            units = -abs(units)

        # Calculate SL/TP prices
        if side in ("buy",):
            sl_price = entry_price * (1 - stop_loss_pct / 100)
            tp_price = entry_price * (1 + take_profit_pct / 100)
        else:
            sl_price = entry_price * (1 + stop_loss_pct / 100)
            tp_price = entry_price * (1 - take_profit_pct / 100)

        if self.dry_run:
            order_id = self._new_client_order_id(symbol)
            fill_price = entry_price
            filled_units = abs(units)
        else:
            result = self._submit_order(instrument, units, sl_price, tp_price)
            fill_str = result.get("orderFillTransaction", {})
            order_id = fill_str.get("id", self._new_client_order_id(symbol))
            fill_price = float(fill_str.get("price", entry_price))
            filled_units = abs(int(fill_str.get("units", units)))

        self.state.record_trade_open(
            order_id, "oanda", symbol, side, filled_units,
            fill_price, sl_price, tp_price,
        )
        self.notifier.notify(
            "trade_opened",
            f"OANDA {side.upper()} {filled_units} {instrument} @ {fill_price:.5f} "
            f"(SL: {sl_price:.5f}, TP: {tp_price:.5f}) "
            f"[{'DRY-RUN' if self.dry_run else 'LIVE'}]",
        )
        return order_id

    def close_trade(self, client_order_id: str, exit_price: float):
        positions = {p["client_order_id"]: p for p in self.state.get_open_positions()}
        pos = positions.get(client_order_id)
        if not pos:
            return

        if not self.dry_run:
            instrument = self._normalize_instrument(pos["symbol"])
            close_units = -int(pos["amount"]) if pos["side"] == "buy" else int(pos["amount"])
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
            resp = requests.post(url, headers=self._headers(), json={
                "order": {
                    "type": "MARKET",
                    "instrument": instrument,
                    "units": str(close_units),
                    "timeInForce": "FOK",
                }
            }, timeout=15)
            resp.raise_for_status()

        direction = 1 if pos["side"] == "buy" else -1
        pnl = direction * (exit_price - pos["entry_price"]) * pos["amount"]

        self.state.record_trade_close(client_order_id, exit_price, pnl)
        self.risk.on_trade_closed(pnl)
