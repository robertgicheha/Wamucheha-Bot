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

from core.fee_manager import FeeModel


class OandaExecutor:
    def __init__(self, api_key: str, account_id: str, state_manager, risk_manager,
                 notifier, practice: bool = False, dry_run: bool = True, fee_model=None):
        self.state = state_manager
        self.risk = risk_manager
        self.notifier = notifier
        self.dry_run = dry_run
        self.api_key = api_key
        self.account_id = account_id
        self.base_url = ("https://api-fxpractice.oanda.com" if practice
                         else "https://api-fxtrade.oanda.com")
        # OANDA's cost is the spread, already inside the fill price, and it
        # charges no separate commission — so the configured rate is 0. Wiring
        # it through FeeModel keeps the reported zero a stated figure and lets
        # an operator who models spread as a bps cost have it deducted.
        self.fees = fee_model or FeeModel()
        self._instrument_info = None

    def _precision(self, instrument: str) -> int:
        """Price decimals OANDA accepts for this instrument (JPY pairs: 3,
        most majors: 5). Sending more is rejected as PRICE_PRECISION_EXCEEDED."""
        if self._instrument_info is None:
            try:
                resp = requests.get(f"{self.base_url}/v3/accounts/{self.account_id}/instruments",
                                    headers=self._headers(), timeout=15)
                resp.raise_for_status()
                self._instrument_info = {i["name"]: i for i in resp.json()["instruments"]}
            except Exception:
                return 3 if "JPY" in instrument else 5
        info = self._instrument_info.get(instrument)
        return int(info["displayPrecision"]) if info else 5

    def _mid(self, instrument: str) -> float | None:
        try:
            resp = requests.get(f"{self.base_url}/v3/accounts/{self.account_id}/pricing",
                                headers=self._headers(), params={"instruments": instrument}, timeout=10)
            resp.raise_for_status()
            p = resp.json()["prices"][0]
            return (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2
        except Exception:
            return None

    def _usd_per_base(self, instrument: str, price: float) -> float | None:
        """USD value of one unit (one unit = one unit of the base currency)."""
        base, quote = instrument.split("_")
        if base == "USD":
            return 1.0
        if quote == "USD":
            return price
        direct = self._mid(f"{base}_USD")
        if direct:
            return direct
        inverse = self._mid(f"USD_{base}")
        return 1 / inverse if inverse else None

    def _quote_to_usd(self, instrument: str, amount_in_quote: float, price: float) -> float:
        """Convert a P&L in the quote currency to USD (used for dry-run estimates)."""
        base, quote = instrument.split("_")
        if quote == "USD":
            return amount_in_quote
        if base == "USD":
            return amount_in_quote / price
        usd_per_base = self._usd_per_base(instrument, price)
        return amount_in_quote * usd_per_base / price if usd_per_base else amount_in_quote

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
        digits = self._precision(instrument)
        order_body = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",  # Fill or Kill — no partial fills lingering
        }

        if stop_loss:
            order_body["stopLossOnFill"] = {
                "price": f"{stop_loss:.{digits}f}",
                "timeInForce": "GTC",
            }
        if take_profit:
            order_body["takeProfitOnFill"] = {
                "price": f"{take_profit:.{digits}f}",
            }

        url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
        resp = requests.post(url, headers=self._headers(),
                            json={"order": order_body}, timeout=15)
        if resp.status_code == 400:
            raise ValueError(f"OANDA rejected order: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()

    def open_trade(self, symbol: str, side: str, proposed_amount: float,
                    entry_price: float, stop_loss_pct: float, take_profit_pct: float,
                    strategies: list = None, score: float = 0, regime: str = ""):
        """
        proposed_amount is in USD (notional). OANDA needs units, so we convert:
        units = floor(notional_value / current_price)
        For forex, 1 unit of EUR/USD ≈ 1.10 USD (the quote price).
        """
        decision = self.risk.pre_trade_check(proposed_amount, symbol=symbol, venue="oanda")
        if not decision.allowed:
            self.notifier.notify("trade_rejected", f"{symbol} {side} rejected: {decision.reason}")
            return None

        instrument = self._normalize_instrument(symbol)

        # Convert USD notional to units of the base currency (USD/JPY: 1 unit
        # = $1; EUR/USD: 1 unit = price USD; crosses via the base's USD rate).
        usd_per_unit = self._usd_per_base(instrument, entry_price) if entry_price > 0 else None
        if not usd_per_unit:
            return None
        units = int(decision.position_size / usd_per_unit)

        if units == 0:
            self.notifier.notify("trade_rejected",
                f"{symbol} {side} skipped: ${decision.position_size:.2f} is less than 1 unit (${usd_per_unit:.2f})")
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
            try:
                result = self._submit_order(instrument, units, sl_price, tp_price)
            except Exception as e:
                self.notifier.notify("trade_rejected", f"OANDA {symbol} order failed: {e}")
                return None
            fill_str = result.get("orderFillTransaction")
            if not fill_str:
                # 201 but cancelled (market closed, FOK not filled, margin, ...)
                reason = (result.get("orderCancelTransaction") or {}).get("reason", "no fill")
                self.notifier.notify("trade_rejected", f"OANDA {symbol} order cancelled: {reason}")
                return None
            # The trade ID is what trades/{id}/close needs.
            order_id = (fill_str.get("tradeOpened") or {}).get("tradeID") or fill_str["id"]
            fill_price = float(fill_str.get("price", entry_price))
            filled_units = abs(int(float(fill_str.get("units", units))))

        entry_notional = float(filled_units) * float(usd_per_unit or 0.0)
        entry_fee = self.fees.cost_for_fill("oanda", symbol, entry_notional)["cost"]
        fee_rate = self.fees.rate_for("oanda", symbol)

        self.state.record_trade_open(
            order_id, "oanda", symbol, side, filled_units,
            fill_price, sl_price, tp_price,
            strategies=strategies, score=score, regime=regime,
            entry_fee=entry_fee, fee_rate=fee_rate,
        )
        self.notifier.notify_trade_opened(
            symbol=symbol, side=side, amount=filled_units,
            entry_price=fill_price, stop_loss=sl_price,
            take_profit=tp_price, exchange="oanda",
            dry_run=self.dry_run, strategies=strategies,
            score=score, regime=regime,
            entry_fee=entry_fee,
            round_trip_fee=self.fees.cost_estimate("oanda", symbol, entry_notional),
        )
        return order_id

    def close_trade(self, client_order_id: str, exit_price: float, reason: str = ""):
        positions = {p["client_order_id"]: p for p in self.state.get_open_positions()}
        pos = positions.get(client_order_id)
        if not pos:
            return

        instrument = self._normalize_instrument(pos["symbol"])
        direction = 1 if pos["side"] == "buy" else -1

        # ── Gross vs net ───────────────────────────────────────────────
        # `amount` is units of the base currency, so the price move is already
        # in the quote currency and only needs converting to the USD the
        # balance is tracked in.
        usd_per_unit = self._usd_per_base(instrument, exit_price) or 0.0
        exit_notional = float(pos["amount"]) * usd_per_unit
        gross_pnl = self._quote_to_usd(
            instrument,
            direction * (exit_price - pos["entry_price"]) * pos["amount"],
            exit_price,
        )

        entry_fee = float(pos.get("entry_fee") or 0.0)
        exit_fee_info = self.fees.cost_for_fill("oanda", pos["symbol"], exit_notional)
        exit_fee = exit_fee_info["cost"]

        if not self.dry_run:
            try:
                # Closing the trade also cancels its attached SL/TP orders.
                url = f"{self.base_url}/v3/accounts/{self.account_id}/trades/{client_order_id}/close"
                resp = requests.put(url, headers=self._headers(), timeout=15)
                broker_pnl = None
                if resp.status_code >= 400:
                    # Already closed at the broker by its SL/TP? Then use OANDA's realized P&L.
                    trade = requests.get(f"{self.base_url}/v3/accounts/{self.account_id}/trades/{client_order_id}",
                                         headers=self._headers(), timeout=15).json().get("trade", {})
                    if trade.get("state") != "CLOSED":
                        raise ValueError(resp.text[:200])
                    broker_pnl = float(trade.get("realizedPL", gross_pnl))
                    exit_price = float(trade.get("averageClosePrice", exit_price))
                else:
                    resp.raise_for_status()
                    fill = resp.json().get("orderFillTransaction")
                    if not fill:
                        reason = (resp.json().get("orderCancelTransaction") or {}).get("reason", "no fill")
                        raise ValueError(f"close cancelled: {reason}")
                    broker_pnl = float(fill.get("pl", gross_pnl))  # account currency (USD)
                    exit_price = float(fill.get("price", exit_price))

                if broker_pnl is not None:
                    # OANDA's realized figure is already net of the spread, so
                    # it drives the balance and the risk state. The gap to the
                    # price move is the cost actually taken, floored at zero.
                    measured_cost = gross_pnl - broker_pnl
                    if measured_cost > 0:
                        gross_pnl = broker_pnl + measured_cost
                        exit_fee = measured_cost
                    pnl = broker_pnl
                else:
                    pnl = gross_pnl - entry_fee - exit_fee
            except Exception as e:
                # Leave the position open so the monitor retries next tick.
                self.notifier.notify("warning", f"Failed to close {pos['symbol']} on OANDA: {e}",
                                     priority="high")
                return
        else:
            pnl = gross_pnl - entry_fee - exit_fee

        # Get trade metadata
        from sqlalchemy.orm import Session
        from core.state_manager import TradeRow, engine
        strategies = None
        with Session(engine) as session:
            trade = session.query(TradeRow).filter_by(client_order_id=client_order_id).first()
            if trade and trade.strategies:
                import json
                strategies = json.loads(trade.strategies)

        self.state.record_trade_close(client_order_id, exit_price, pnl, reason,
                                      exit_fee=exit_fee,
                                      fees_are_estimated=exit_fee_info["estimated"])
        self.risk.on_trade_closed(pnl)

        self.notifier.notify_trade_closed(
            symbol=pos["symbol"], side=pos["side"], amount=pos["amount"],
            entry_price=pos["entry_price"], exit_price=exit_price,
            pnl=pnl, exchange="oanda", reason=reason,
            strategies=strategies,
            gross_pnl=gross_pnl, entry_fee=entry_fee, exit_fee=exit_fee,
            fees_are_estimated=exit_fee_info["estimated"],
            opened_at=pos.get("opened_at"),
        )
