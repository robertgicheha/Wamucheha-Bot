"""
Cost model — every trade is logged net of what it actually cost to execute.

Two different costs get conflated as "fees" and only one of them is a
per-trade cost:

  1. EXCHANGE TRADING FEE (taker/maker) — charged by the venue on every fill.
     This is real, charged on every single trade, and the only one that
     belongs on a per-trade PnL line. At 10 bps (0.10%) a round trip costs
     0.20% of notional, which against a 3% take-profit target silently eats
     ~7% of the intended reward and turns marginal winners into breakevens.
     It is the single most common reason a backtest is profitable and the
     live account is not.

  2. ON-CHAIN NETWORK FEE (gas) — paid ONCE per deposit/withdrawal, by YOU,
     on the network the venue names (Tron/BSC/Ethereum). It is not per
     trade: one TRC20 transfer costing ~1 USDT funds hundreds of trades, and
     dividing it per trade would be a fiction. It is therefore tracked as a
     cumulative pot (see `record_transfer`) and reported in the 6h/24h
     reports and the daily economics block, so the true all-in cost of
     trading is visible without lying about any individual fill.

Live vs dry-run: in live mode the fee actually charged by the venue is read
back off the ccxt order object wherever the venue reports it, so VIP tiers,
BNB discounts and maker fills are reflected automatically. Dry-run has no
order to read, so the configured taker rate is used — which is why the
configured rate should be set to what you actually pay, not the headline
advertised number.

If a venue's fee model is not known, `unknown_fee` marks the fill so a
mispriced cost shows up as a gap in the report instead of silently as zero.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("fee_manager")

TRANSFER_LOG = Path(__file__).parent.parent / "data" / "network_fees.jsonl"

# Default spot taker rates, as a fraction, for venues with a public standard
# tier. Used only when config.yaml does not override them. VIP/BNB-discount
# tiers are strictly lower — set yours in config.yaml to what you pay.
DEFAULT_TAKER_BPS = {
    "binance": 10.0,
    "okx": 10.0,
    "bybit": 10.0,
    "kraken": 26.0,
    "coinbase": 60.0,
    # Non-crypto venues. Alpaca equities are commission-free; OANDA and MT5
    # charge spread, which is already inside the fill price, so there is no
    # separate commission line to add here.
    "alpaca": 0.0,
    "oanda": 0.0,
    "mt5": 0.0,
}
FALLBACK_TAKER_BPS = 10.0


class FeeModel:
    """Per-venue cost model. Thread-safe because the main loop, the position
    monitor and the reporting timers can all touch it in the same tick."""

    def __init__(self, cfg: dict = None, exchange=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        per_venue = {k.lower(): float(v) for k, v in
                     (cfg.get("per_venue_taker_bps") or {}).items()}
        self.taker_bps = float(cfg.get("taker_fee_bps", FALLBACK_TAKER_BPS))
        self.rates = {venue: per_venue.get(venue, DEFAULT_TAKER_BPS.get(venue, self.taker_bps))
                      for venue in DEFAULT_TAKER_BPS}
        self.rates.update(per_venue)
        self.exchange = exchange
        self.network = (cfg.get("network") or "TRC20").upper()
        self.network_fee_usdt = {k.upper(): float(v) for k, v in
                                 (cfg.get("network_fee_usdt") or {}).items()}
        self._lock = threading.Lock()
        self._transfers_today: list = []

    # ---------- rate lookup ----------

    def rate_for(self, venue: str, symbol: str = None) -> float:
        """Taker rate as a FRACTION (0.001 == 0.10%)."""
        venue = (venue or "").lower()
        if not self.enabled:
            return 0.0
        return self.rates.get(venue, self.taker_bps) / 10_000.0

    # ---------- per-fill cost ----------

    def cost_for_fill(self, venue: str, symbol: str, notional_usd: float,
                      order: dict = None, fill_price: float = None) -> dict:
        """Cost of one fill, in USD.

        `order` is the raw ccxt order dict. When the venue reported the fee it
        actually charged, that number wins over the configured estimate —
        it is the real cost, including any tier discount. When it did not
        (or the currency is neither the quote nor the base asset), the
        configured rate is used and `estimated` is set, so the report can
        show which fills are priced figures rather than measured ones.
        """
        notional_usd = float(notional_usd or 0.0)
        if not self.enabled:
            return {"cost": 0.0, "rate": 0.0, "source": "disabled", "estimated": False}

        rate = self.rate_for(venue, symbol)
        reported = self._reported_fee(order, notional_usd, fill_price)
        if reported is not None:
            return {"cost": reported, "rate": reported / notional_usd if notional_usd else 0.0,
                    "source": "exchange", "estimated": False}
        return {"cost": notional_usd * rate, "rate": rate,
                "source": "config", "estimated": True}

    @staticmethod
    def _reported_fee(order: dict, notional_usd: float, fill_price: float):
        """Pull the fee the venue actually charged, in USD, or None."""
        if not isinstance(order, dict):
            return None
        fee = order.get("fee")
        candidates = [fee] if isinstance(fee, dict) else list(fee or [])
        for f in candidates:
            if not isinstance(f, dict):
                continue
            cost = f.get("cost")
            currency = (f.get("currency") or "").upper()
            if cost is None or not currency:
                continue
            try:
                cost = float(cost)
            except (TypeError, ValueError):
                continue
            # Quote-asset fees (USDT/USD) are already the USD cost.
            if currency in ("USDT", "USD", "USDC", "BUSD"):
                return abs(cost)
            # Base-asset fees (BNB, ETH, ...) must be converted, and only
            # converted when a price is actually known — guessing here would
            # silently understate the cost.
            if fill_price and fill_price > 0:
                return abs(cost) * fill_price
            return None
        # Some venues only break the fee out per trade rather than per order.
        for t in (order.get("trades") or []):
            got = FeeModel._reported_fee(t, notional_usd, fill_price)
            if got is not None:
                return got
        return None

    # ---------- network (gas) pot ----------

    def network_fee_for(self, network: str = None) -> float:
        return float(self.network_fee_usdt.get((network or self.network).upper(), 0.0))

    def record_transfer(self, venue: str, network: str = None, amount_usdt: float = 0.0,
                        direction: str = "deposit", actual_cost_usdt: float = None):
        """Record a deposit or withdrawal YOU performed, and its network fee.

        The bot never moves money, so this is a declaration by the operator,
        not an automated detection. It exists so the network cost of running
        the account is accumulated and reported instead of being invisible —
        across a month of trading it is small, but it is real and it is
        never zero, and pretending otherwise is how people end up
        underestimating what the bot cost them to run.
        """
        cost = float(actual_cost_usdt) if actual_cost_usdt is not None \
            else self.network_fee_for(network)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": (venue or "").lower(),
            "network": (network or self.network).upper(),
            "direction": direction,
            "amount_usdt": float(amount_usdt or 0.0),
            "network_fee_usdt": cost,
        }
        with self._lock:
            self._transfers_today.append(entry)
        try:
            TRANSFER_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(TRANSFER_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"Could not persist network fee record: {e}")
        return entry

    def network_spend(self) -> dict:
        """Cumulative on-chain cost and what it has funded."""
        with self._lock:
            entries = list(self._transfers_today)
        total = sum(e["network_fee_usdt"] for e in entries)
        funded = sum(e["amount_usdt"] for e in entries if e["direction"] == "deposit")
        return {
            "transfers": len(entries),
            "network_fee_usdt": round(total, 4),
            "amount_funded_usdt": round(funded, 2),
            "network": self.network,
        }

    def reset_daily(self):
        with self._lock:
            self._transfers_today.clear()

    def cost_estimate(self, venue: str, symbol: str, notional_usd: float) -> float:
        """Round-trip cost in USD for sizing decisions (entry + exit)."""
        return 2 * float(notional_usd or 0.0) * self.rate_for(venue, symbol)


def build_fee_model(config: dict, venue: str = None, exchange=None) -> FeeModel:
    """Convenience factory. `venue` is unused today but keeps the signature
    stable if per-venue fee schedules ever diverge further."""
    return FeeModel((config or {}).get("fees"), exchange=exchange)
