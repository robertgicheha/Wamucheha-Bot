"""
Risk & Execution rules manager.

This module is the gatekeeper: NOTHING trades without passing through here first.
Strategy signals are advisory only — this module has final veto power on every
single order. That separation is deliberate: a bug or bad signal in the strategy
layer should never be able to bypass a risk limit.
"""
from dataclasses import dataclass
from datetime import datetime, timezone


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

    # ---------- core gate every order must pass ----------
    def pre_trade_check(self, proposed_amount: float) -> RiskDecision:
        risk_state = self.state.get_risk_state()

        if risk_state["trading_halted"]:
            return RiskDecision(False, f"Trading halted: {risk_state['halt_reason']}")

        self._maybe_reset_daily(risk_state)
        risk_state = self.state.get_risk_state()  # re-read after possible reset

        if risk_state["consecutive_losses"] >= self.cfg["max_consecutive_losses"]:
            self._halt(f"{self.cfg['max_consecutive_losses']} consecutive losses reached")
            return RiskDecision(False, "Consecutive loss limit hit")

        daily_loss_limit = -abs(risk_state["trading_balance"] * self.cfg["max_daily_loss_pct"] / 100)
        if risk_state["daily_pnl"] <= daily_loss_limit:
            self._halt("Daily loss limit reached", until_tomorrow=True)
            return RiskDecision(False, "Daily loss limit hit")

        if risk_state["trading_balance"] <= 0:
            return RiskDecision(False, "No trading balance — profit buffer exhausted. "
                                        "Stake is protected and untouched.")

        # position sizing: cap at max_position_pct of TRADING balance only, never the stake
        max_size = risk_state["trading_balance"] * self.cfg["max_position_pct"] / 100
        size = min(proposed_amount, max_size)
        if size <= 0:
            return RiskDecision(False, "Computed position size is zero")

        return RiskDecision(True, "OK", position_size=size)

    # ---------- called after every trade closes ----------
    def on_trade_closed(self, pnl: float):
        risk_state = self.state.get_risk_state()
        new_balance = risk_state["trading_balance"] + pnl  # pnl already excludes stake
        new_daily_pnl = risk_state["daily_pnl"] + pnl

        if pnl > 0:
            new_streak = 0
        else:
            new_streak = risk_state["consecutive_losses"] + 1

        self.state.update_risk_state(
            trading_balance=max(new_balance, 0),
            consecutive_losses=new_streak,
            daily_pnl=new_daily_pnl,
        )

        self.notifier.notify(
            "trade_closed",
            f"Trade closed. PnL: {pnl:+.2f} USD. Trading balance: {max(new_balance,0):.2f}. "
            f"Consecutive losses: {new_streak}/{self.cfg['max_consecutive_losses']}. "
            f"Stake ({self.stake_amount} USD) untouched.",
        )

        if new_streak >= self.cfg["max_consecutive_losses"]:
            self._halt(f"{new_streak} consecutive losses reached")

        self._maybe_sweep_profit()

    # ---------- profit sweeping per your compounding spec ----------
    def _maybe_sweep_profit(self):
        risk_state = self.state.get_risk_state()
        threshold = self.cfg["profit_withdrawal_threshold"]
        keep = self.cfg["profit_withdrawal_keep"]
        if risk_state["trading_balance"] >= threshold:
            swept = risk_state["trading_balance"] - keep
            self.state.update_risk_state(trading_balance=keep)
            self.notifier.notify(
                "profit_swept_to_stake",
                f"Trading balance hit {risk_state['trading_balance']:.2f}. "
                f"Swept {swept:.2f} USD to stake wallet (manual transfer required — "
                f"the bot does NOT have withdrawal permissions by design). "
                f"Trading continues with {keep:.2f} USD.",
            )
            # NOTE: actual withdrawal is a manual step or a separate, narrowly-scoped
            # withdrawal-permission key. Never give the trading API key withdrawal rights.

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
        """Deliberately requires an explicit human call — never auto-resumes."""
        self.state.update_risk_state(trading_halted=0, halt_reason=None, consecutive_losses=0)
        self.notifier.notify("circuit_breaker_reset", f"Trading resumed by {actor}.")

    def _maybe_reset_daily(self, risk_state):
        today = datetime.now(timezone.utc).date().isoformat()
        if risk_state["daily_reset_at"] != today:
            self.state.update_risk_state(daily_pnl=0, daily_reset_at=today)

    # ---------- volatility circuit breaker ----------
    def check_volatility(self, pct_move: float) -> bool:
        """Return True if it's safe to trade; False if volatility breaker should pause."""
        if abs(pct_move) >= self.cfg["volatility_circuit_breaker_pct"]:
            self._halt(f"Volatility circuit breaker: {pct_move:.2f}% move detected")
            return False
        return True
