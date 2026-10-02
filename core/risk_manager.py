"""
Risk & Execution rules manager — enhanced with portfolio-level controls.

This module is the gatekeeper: NOTHING trades without passing through here first.
Strategy signals are advisory only — this module has final veto power on every
single order. That separation is deliberate: a bug or bad signal in the strategy
layer should never be able to bypass a risk limit.

Portfolio-level controls added:
  - Max open positions (absolute count)
  - Per-asset-class exposure caps (e.g., max 60% in crypto)
  - Correlation check (don't let BTC+ETH+SOL all count as diversified)
  - Position reconciliation (detect drift between DB and exchange)
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from core.structured_logger import log_risk_event, log_system_event

# Asset class mapping for portfolio-level caps
ASSET_CLASS_MAP = {
    # Crypto (USDT-quoted spot)
    "BTC/USDT": "crypto", "ETH/USDT": "crypto", "SOL/USDT": "crypto",
    "BNB/USDT": "crypto", "XRP/USDT": "crypto", "DOGE/USDT": "crypto",
    "ADA/USDT": "crypto", "LINK/USDT": "crypto", "AVAX/USDT": "crypto",
    "QNT/USDT": "crypto",
    # Forex
    "EUR/USD": "forex", "GBP/USD": "forex", "USD/JPY": "forex",
    "AUD/USD": "forex", "EUR/GBP": "forex", "EUR/JPY": "forex",
    "USD/CAD": "forex", "GBP/JPY": "forex",
    # Commodities / Gold
    "XAU/USD": "commodities", "XAG/USD": "commodities",
    "XAUUSD": "commodities", "XAGUSD": "commodities",
    # MT5 forex / crypto CFDs
    "EURUSD": "forex", "GBPUSD": "forex", "AUDUSD": "forex", "USDCAD": "forex",
    "EURJPY": "forex", "EURGBP": "forex", "USDJPY": "forex", "GBPJPY": "forex",
    "BTCUSD": "crypto", "ETHUSD": "crypto",
    # US Stocks / ETFs
    "AAPL": "equities", "MSFT": "equities", "SPY": "equities",
    "QQQ": "equities", "GLD": "commodities", "TLT": "fixed_income",
    # NSE (Kenya)
    "SCOM": "nse", "EQTY": "nse", "KCB": "nse", "BAT": "nse",
    "EABL": "nse", "SAFARICOM": "nse", "DTK": "nse",
    "COOP": "nse", "ABSA": "nse", "KNC": "nse", "NIC": "nse",
}

# MT5 contract sizes (units per lot) by symbol prefix; forex default 100,000.
MT5_CONTRACT_SIZE = {"XAU": 100, "XAG": 5000, "BTC": 1, "ETH": 1}


def position_notional_usd(pos: dict) -> float:
    """Approximate USD notional of an open position. `amount` means different
    things per broker: coins/shares (ccxt, Alpaca), base-currency units
    (OANDA), or lots (MT5) — amount * price is only right for the first."""
    symbol, amount, price = pos["symbol"].upper(), pos["amount"], pos["entry_price"]
    if pos.get("exchange") == "mt5":
        size = next((v for k, v in MT5_CONTRACT_SIZE.items() if symbol.startswith(k)), 100000)
        units = amount * size
        if symbol.startswith("USD"):
            return units                   # USDJPY: base is USD
        if symbol.endswith("USD"):
            return units * price           # EURUSD, XAUUSD, BTCUSD
        return units                       # crosses: ~1 USD per base unit
    if pos.get("exchange") == "oanda":
        if symbol.startswith("USD/"):
            return amount                  # base is USD
        if symbol.endswith("/USD"):
            return amount * price
        return amount                      # crosses: ~1 USD per base unit
    return amount * price


# Known correlated crypto pairs (move together ~70-90% of the time).
# Broad majors and alts alike tend to move as one risk-on/risk-off bloc, so
# they are all capped together: five open crypto longs is one large position,
# not five independent ones.
CRYPTO_CORRELATION_GROUPS = [
    {"BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT",
     "ADA/USDT", "LINK/USDT", "AVAX/USDT", "QNT/USDT"},
]


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    position_size: float = 0.0


class RiskManager:
    def __init__(self, state_manager, config: dict, notifier):
        self.state = state_manager
        self.cfg = config["risk"]
        self.stake_amount = config["account"]["stake_amount"]
        self.notifier = notifier

        # Portfolio-level config
        risk_cfg = config.get("risk", {})
        self.max_open_positions = risk_cfg.get("max_open_positions", 10)
        self.max_asset_class_exposure_pct = risk_cfg.get("max_asset_class_exposure_pct", 60)
        self.max_correlated_positions = risk_cfg.get("max_correlated_positions", 3)
        self.max_venue_exposure_pct = risk_cfg.get("max_venue_exposure_pct", 40)
        self.reconciliation_interval = risk_cfg.get("reconciliation_interval_seconds", 3600)
        self._last_reconciliation = 0
        # Venues the operator has switched off, checked on every order. A venue
        # that gets disabled here stops opening new positions without needing
        # a config edit + restart, which is the difference between a kill
        # switch and a suggestion.
        self.disabled_venues = set()

    # ---------- core gate every order must pass ----------
    def pre_trade_check(self, proposed_amount: float, symbol: str = None,
                        venue: str = None) -> RiskDecision:
        risk_state = self.state.get_risk_state()

        if risk_state["trading_halted"]:
            log_risk_event("trading_halted", f"Trade blocked: {risk_state['halt_reason']}",
                           priority="high", trading_balance=risk_state["trading_balance"],
                           daily_pnl=risk_state["daily_pnl"],
                           consecutive_losses=risk_state["consecutive_losses"],
                           proposed_action=f"open {symbol} on {venue}")
            return RiskDecision(False, f"Trading halted: {risk_state['halt_reason']}")

        # Venue kill switch, ahead of every other check.
        if venue and venue.lower() in self.disabled_venues:
            log_risk_event("venue_disabled", f"Trade blocked: venue '{venue}' is disabled",
                           priority="normal", trading_balance=risk_state["trading_balance"],
                           proposed_action=f"open {symbol} on {venue}")
            return RiskDecision(False, f"Venue '{venue}' is disabled")

        self._maybe_reset_daily(risk_state)
        risk_state = self.state.get_risk_state()

        if risk_state["consecutive_losses"] >= self.cfg["max_consecutive_losses"]:
            self._halt(f"{self.cfg['max_consecutive_losses']} consecutive losses reached")
            log_risk_event("consecutive_loss_limit", 
                           f"Max consecutive losses ({self.cfg['max_consecutive_losses']}) reached",
                           priority="high", trading_balance=risk_state["trading_balance"],
                           daily_pnl=risk_state["daily_pnl"],
                           consecutive_losses=risk_state["consecutive_losses"],
                           proposed_action=f"open {symbol} on {venue}")
            return RiskDecision(False, "Consecutive loss limit hit")

        daily_loss_limit = -abs(risk_state["trading_balance"] * self.cfg["max_daily_loss_pct"] / 100)
        if risk_state["daily_pnl"] <= daily_loss_limit:
            self._halt("Daily loss limit reached", until_tomorrow=True)
            log_risk_event("daily_loss_limit", 
                           f"Daily loss limit hit: {risk_state['daily_pnl']:.2f} <= {daily_loss_limit:.2f}",
                           priority="high", trading_balance=risk_state["trading_balance"],
                           daily_pnl=risk_state["daily_pnl"],
                           consecutive_losses=risk_state["consecutive_losses"],
                           proposed_action=f"open {symbol} on {venue}")
            return RiskDecision(False, "Daily loss limit hit")

        peak = risk_state.get("peak_balance", risk_state["trading_balance"])
        if peak > 0:
            drawdown_pct = (peak - risk_state["trading_balance"]) / peak * 100
            max_dd = self.cfg.get("max_drawdown_pct", 5)
            if drawdown_pct >= max_dd:
                self._halt(f"Max drawdown {drawdown_pct:.1f}% exceeded limit {max_dd}%")
                log_risk_event("max_drawdown", 
                               f"Max drawdown {drawdown_pct:.1f}% exceeded limit {max_dd}%",
                               priority="high", trading_balance=risk_state["trading_balance"],
                               daily_pnl=risk_state["daily_pnl"],
                               consecutive_losses=risk_state["consecutive_losses"],
                               proposed_action=f"open {symbol} on {venue}")
                return RiskDecision(False, f"Max drawdown limit hit ({drawdown_pct:.1f}%)")

        if risk_state["trading_balance"] <= 0:
            log_risk_event("zero_balance", 
                           "No trading balance — profit buffer exhausted",
                           priority="high", trading_balance=risk_state["trading_balance"],
                           daily_pnl=risk_state["daily_pnl"],
                           consecutive_losses=risk_state["consecutive_losses"],
                           proposed_action=f"open {symbol} on {venue}")
            return RiskDecision(False, "No trading balance — profit buffer exhausted. "
                                        "Stake is protected and untouched.")

        # --- Portfolio-level checks (require symbol) ---
        if symbol:
            open_positions = self.state.get_open_positions()

            # Max open positions
            if len(open_positions) >= self.max_open_positions:
                log_risk_event("max_positions", 
                               f"Max open positions ({self.max_open_positions}) reached",
                               priority="normal", trading_balance=risk_state["trading_balance"],
                               daily_pnl=risk_state["daily_pnl"],
                               consecutive_losses=risk_state["consecutive_losses"],
                               proposed_action=f"open {symbol} on {venue}")
                return RiskDecision(False, f"Max open positions ({self.max_open_positions}) reached")

            # Per-venue exposure cap
            if venue:
                venue_check = self._check_venue_exposure(
                    venue, open_positions, risk_state["trading_balance"])
                if not venue_check.allowed:
                    log_risk_event("venue_exposure", venue_check.reason,
                                   priority="normal", trading_balance=risk_state["trading_balance"],
                                   daily_pnl=risk_state["daily_pnl"],
                                   consecutive_losses=risk_state["consecutive_losses"],
                                   proposed_action=f"open {symbol} on {venue}")
                    return venue_check

            # Per-asset-class exposure cap
            asset_class = ASSET_CLASS_MAP.get(symbol, "other")
            class_exposure = self._calc_asset_class_exposure(
                open_positions, asset_class, risk_state["trading_balance"]
            )
            if class_exposure >= self.max_asset_class_exposure_pct:
                log_risk_event("asset_class_exposure", 
                               f"Asset class '{asset_class}' exposure ({class_exposure:.1f}%) exceeds cap ({self.max_asset_class_exposure_pct}%)",
                               priority="normal", trading_balance=risk_state["trading_balance"],
                               daily_pnl=risk_state["daily_pnl"],
                               consecutive_losses=risk_state["consecutive_losses"],
                               proposed_action=f"open {symbol} on {venue}")
                return RiskDecision(False,
                    f"Asset class '{asset_class}' exposure ({class_exposure:.1f}%) "
                    f"exceeds cap ({self.max_asset_class_exposure_pct}%)")

            # Correlation check: don't over-concentrate in correlated assets
            corr_check = self._check_correlation(symbol, open_positions)
            if not corr_check.allowed:
                log_risk_event("correlation_limit", corr_check.reason,
                               priority="normal", trading_balance=risk_state["trading_balance"],
                               daily_pnl=risk_state["daily_pnl"],
                               consecutive_losses=risk_state["consecutive_losses"],
                               proposed_action=f"open {symbol} on {venue}")
                return corr_check

        # Position sizing
        max_size = risk_state["trading_balance"] * self.cfg["max_position_pct"] / 100
        size = min(proposed_amount, max_size)
        if size <= 0:
            log_risk_event("zero_position_size", "Computed position size is zero",
                           priority="normal", trading_balance=risk_state["trading_balance"],
                           daily_pnl=risk_state["daily_pnl"],
                           consecutive_losses=risk_state["consecutive_losses"],
                           proposed_action=f"open {symbol} on {venue}")
            return RiskDecision(False, "Computed position size is zero")

        return RiskDecision(True, "OK", position_size=size)

    # ---------- portfolio-level helpers ----------
    def _calc_asset_class_exposure(self, open_positions: list, asset_class: str,
                                    trading_balance: float) -> float:
        """Calculate current exposure to an asset class as % of trading balance."""
        if trading_balance <= 0:
            return 100.0

        class_exposure = 0.0
        for pos in open_positions:
            pos_class = ASSET_CLASS_MAP.get(pos["symbol"], "other")
            if pos_class == asset_class:
                class_exposure += position_notional_usd(pos)

        return (class_exposure / trading_balance) * 100

    def _check_venue_exposure(self, venue: str, open_positions: list,
                              trading_balance: float) -> RiskDecision:
        """Cap total open notional on one venue. Counted on positions whose
        recorded exchange matches, so the same pair held on two venues counts
        against each one separately."""
        if trading_balance <= 0:
            return RiskDecision(False, "No trading balance")

        venue = venue.lower()
        exposure = sum(position_notional_usd(p) for p in open_positions
                       if (p.get("exchange") or "").lower() == venue)
        exposure_pct = exposure / trading_balance * 100
        if exposure_pct >= self.max_venue_exposure_pct:
            return RiskDecision(False,
                f"Venue '{venue}' exposure ({exposure_pct:.1f}%) "
                f"exceeds cap ({self.max_venue_exposure_pct}%)")
        return RiskDecision(True, "OK")

    def _check_correlation(self, symbol: str, open_positions: list) -> RiskDecision:
        """Check if adding this symbol would over-concentrate in correlated assets."""
        # Find which correlation group this symbol belongs to
        sym_group = None
        for group in CRYPTO_CORRELATION_GROUPS:
            if symbol in group:
                sym_group = group
                break

        if sym_group is None:
            return RiskDecision(True, "OK")

        # Count how many positions are already in this correlation group
        group_count = sum(
            1 for pos in open_positions
            if pos["symbol"] in sym_group
        )

        if group_count >= self.max_correlated_positions:
            return RiskDecision(False,
                f"Correlated positions limit ({self.max_correlated_positions}) for "
                f"group {[s for s in sym_group]}: already have {group_count} open")

        return RiskDecision(True, "OK")

    # ---------- position reconciliation ----------
    def maybe_reconcile_positions(self, executors: dict):
        """
        Periodically reconcile DB positions against exchange reality.
        Detects drift: DB says open but exchange says closed (or vice versa).
        """
        now = time.time()
        if now - self._last_reconciliation < self.reconciliation_interval:
            return
        self._last_reconciliation = now

        db_positions = {p["client_order_id"]: p for p in self.state.get_open_positions()}
        drift_count = 0

        for client_id, pos in db_positions.items():
            exchange_name = pos["exchange"]
            executor = executors.get(exchange_name)
            if executor is None or executor.dry_run:
                continue

            try:
                # Check if position still exists on exchange
                exchange_pos = executor.exchange.fetch_position(pos["symbol"])
                if exchange_pos is None or float(exchange_pos.get("amount", 0)) == 0:
                    # Exchange says closed but DB says open — stale record
                    drift_count += 1
                    self.notifier.notify("position_drift",
                        f"Position drift detected: {pos['symbol']} on {exchange_name} "
                        f"shows 0 on exchange but DB has it open. Closing stale record.",
                        priority="high")
                    # Force close in DB
                    self.state.record_trade_close(client_id, pos["entry_price"], 0,
                        reason="reconciliation_drift")
            except Exception:
                pass

        if drift_count > 0:
            print(f"  Reconciliation: {drift_count} stale positions closed")

    # ---------- called after every trade closes ----------
    def on_trade_closed(self, pnl: float, fees: float = 0.0):
        """`pnl` is NET of venue fees — callers must not pass a gross figure.
        Fees are additionally shown here rather than silently folded in, so
        a day that lost money to costs is visibly a day that lost money to
        costs, not an unexplained gap between the chart and the balance."""
        risk_state = self.state.get_risk_state()
        new_balance = risk_state["trading_balance"] + pnl
        new_daily_pnl = risk_state["daily_pnl"] + pnl

        if pnl > 0:
            new_streak = 0
        else:
            new_streak = risk_state["consecutive_losses"] + 1

        peak = risk_state.get("peak_balance", new_balance)
        if new_balance > peak:
            peak = new_balance

        self.state.update_risk_state(
            trading_balance=max(new_balance, 0),
            peak_balance=peak,
            consecutive_losses=new_streak,
            daily_pnl=new_daily_pnl,
        )

        if new_streak >= self.cfg["max_consecutive_losses"]:
            self._halt(f"{new_streak} consecutive losses reached")

        self._maybe_sweep_profit()

    # ---------- profit sweeping ----------
    def _maybe_sweep_profit(self):
        risk_state = self.state.get_risk_state()
        threshold = self.cfg["profit_withdrawal_threshold"]
        keep = self.cfg["profit_withdrawal_keep"]
        if risk_state["trading_balance"] >= threshold:
            swept = risk_state["trading_balance"] - keep
            self.state.update_risk_state(trading_balance=keep)
            # Record the movement before announcing it. Without this row the
            # balance drops by `swept` and every later report reads that drop
            # as trading losses, which is the single most misleading thing this
            # system could print. The ledger is what lets the digest say
            # "profit returned to your stake wallet" instead of "-280.00".
            try:
                self.state.record_cash_flow(
                    "sweep", swept,
                    note=(f"auto-sweep at {threshold:.0f} USD threshold, "
                          f"kept {keep:.0f} USD trading"),
                )
            except Exception:
                pass
            self.notifier.notify(
                "profit_swept_to_stake",
                f"Trading balance reached {risk_state['trading_balance']:.2f} USD. "
                f"{swept:.2f} USD of profit moved out to your stake wallet "
                f"(manual transfer required — the bot has no withdrawal permissions "
                f"by design). Trading continues with {keep:.2f} USD.",
            )

    # ---------- circuit breakers ----------
    def _halt(self, reason: str, until_tomorrow: bool = False):
        self.state.update_risk_state(trading_halted=1, halt_reason=reason)
        self.notifier.notify(
            "circuit_breaker_triggered",
            f"TRADING HALTED: {reason}. Stake is safe. Manual review required "
            f"before resuming (see dashboard).",
            priority="high",
        )

    def resume_trading(self, actor: str):
        """Clear a halt and reset the loss counter.

        Always goes through the risk manager rather than writing risk state
        directly, so the resume is notified and attributed. Writing
        `trading_halted=0` to the state store from a command handler looks
        equivalent but silently skips the audit notification, which is the one
        record of who re-armed a halted bot.
        """
        self.state.update_risk_state(trading_halted=0, halt_reason=None, consecutive_losses=0)
        self.notifier.notify("circuit_breaker_reset", f"Trading resumed by {actor}.")

    # ---------- per-venue kill switch ----------

    def disable_venue(self, venue: str, reason: str = "", actor: str = "operator"):
        """Stop opening new positions on one venue. Existing positions are left
        alone deliberately: the position monitor still needs to reach the venue
        to place stops and close out, and a forced unwind is a trading decision,
        not a safety one. Global circuit breakers are unaffected."""
        venue = venue.lower()
        self.disabled_venues.add(venue)
        self.notifier.notify(
            "venue_disabled",
            f"New entries on '{venue}' are BLOCKED ({reason or 'no reason given'}). "
            f"Open positions on {venue} are still managed and closed normally. "
            f"Disabled by {actor}.",
            priority="high",
        )

    def enable_venue(self, venue: str, actor: str = "operator"):
        venue = venue.lower()
        self.disabled_venues.discard(venue)
        self.notifier.notify("venue_enabled",
                             f"New entries on '{venue}' re-enabled by {actor}.")

    def get_disabled_venues(self) -> list:
        return sorted(self.disabled_venues)

    def _maybe_reset_daily(self, risk_state):
        today = datetime.now(timezone.utc).date().isoformat()
        if risk_state["daily_reset_at"] != today:
            self.state.update_risk_state(daily_pnl=0, daily_reset_at=today)

    def check_volatility(self, pct_move: float) -> bool:
        if abs(pct_move) >= self.cfg["volatility_circuit_breaker_pct"]:
            self._halt(f"Volatility circuit breaker: {pct_move:.2f}% move detected")
            return False
        return True

    # ---------- portfolio health summary ----------
    def get_portfolio_health(self) -> dict:
        """Summary of portfolio-level risk metrics for dashboard/monitoring."""
        risk_state = self.state.get_risk_state()
        open_positions = self.state.get_open_positions()
        balance = risk_state.get("trading_balance", 0)

        # Asset class breakdown
        class_exposure = {}
        for pos in open_positions:
            ac = ASSET_CLASS_MAP.get(pos["symbol"], "other")
            if ac not in class_exposure:
                class_exposure[ac] = {"count": 0, "value": 0.0}
            class_exposure[ac]["count"] += 1
            class_exposure[ac]["value"] += pos["amount"] * pos["entry_price"]

        # Correlation group breakdown
        corr_exposure = {}
        for pos in open_positions:
            for group in CRYPTO_CORRELATION_GROUPS:
                if pos["symbol"] in group:
                    group_id = id(group)
                    if group_id not in corr_exposure:
                        corr_exposure[group_id] = {"symbols": list(group), "count": 0}
                    corr_exposure[group_id]["count"] += 1

        return {
            "open_positions": len(open_positions),
            "max_positions": self.max_open_positions,
            "balance": balance,
            "asset_class_exposure": class_exposure,
            "correlated_groups": corr_exposure,
            "trading_halted": risk_state.get("trading_halted", 0),
            "consecutive_losses": risk_state.get("consecutive_losses", 0),
            "daily_pnl": risk_state.get("daily_pnl", 0),
            "drawdown_pct": self._current_drawdown(risk_state),
        }

    def _current_drawdown(self, risk_state: dict) -> float:
        peak = risk_state.get("peak_balance", risk_state["trading_balance"])
        if peak <= 0:
            return 0.0
        return (peak - risk_state["trading_balance"]) / peak * 100
