"""
Profit-split policy: compound most profit, pay out the rest.

  * first run only records the baseline, nothing is paid
  * profit above the baseline is split payout_pct / compounded, and the
    baseline resets to the post-payout balance
  * a drawdown is not paid out as it recovers (high-water mark)
  * slices under payout_min_usd are held back
  * payout_pct 0 compounds everything; 100 pays out everything
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["WAMUCHEHA_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="wmc-split-")) / "state.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.risk_manager import RiskManager  # noqa: E402
from core.state_manager import StateManager  # noqa: E402


class _Notifier:
    def __init__(self):
        self.sent = []

    def notify(self, kind, msg, priority="normal"):
        self.sent.append((kind, msg))


def _make(balance=1000.0, **risk):
    sm = StateManager(stake_amount=1.0, initial_trading_balance=balance)
    sm.update_risk_state(trading_balance=balance, payout_baseline=None, trading_halted=0,
                         consecutive_losses=0, daily_pnl=0.0)
    cfg = {"account": {"stake_amount": 1.0},
           "risk": {"max_consecutive_losses": 99, "max_daily_loss_pct": 99, "max_drawdown_pct": 99,
                    "max_position_pct": 3, "profit_policy": "split", "payout_pct": 50,
                    "payout_min_usd": 5, "profit_withdrawal_threshold": 500,
                    "profit_withdrawal_keep": 200, **risk}}
    n = _Notifier()
    return RiskManager(sm, cfg, n), sm, n


def _set(sm, bal):
    sm.update_risk_state(trading_balance=bal)


def test_first_run_sets_baseline_and_pays_nothing():
    rm, sm, n = _make()
    rm._maybe_sweep_profit()
    assert sm.get_risk_state()["payout_baseline"] == 1000.0 and not n.sent


def test_profit_is_split_and_baseline_resets():
    rm, sm, n = _make()
    base_sweeps = sm.get_cash_flow_totals()["swept"]
    rm._maybe_sweep_profit()
    _set(sm, 1100.0)                     # +100 profit
    rm._maybe_sweep_profit()
    rs = sm.get_risk_state()
    assert rs["trading_balance"] == 1050.0       # 50 paid out, 50 compounded
    assert rs["payout_baseline"] == 1050.0
    assert n.sent and "50.00" in n.sent[0][1]
    assert sm.get_cash_flow_totals()["swept"] - base_sweeps == 50.0


def test_drawdown_recovery_is_not_paid_out():
    rm, sm, n = _make()
    rm._maybe_sweep_profit()
    _set(sm, 900.0)
    rm._maybe_sweep_profit()
    _set(sm, 1000.0)                     # back to baseline: no new profit
    rm._maybe_sweep_profit()
    assert sm.get_risk_state()["trading_balance"] == 1000.0 and not n.sent
    _set(sm, 1020.0)                     # genuinely new profit
    rm._maybe_sweep_profit()
    assert sm.get_risk_state()["trading_balance"] == 1010.0


def test_small_slice_is_held_back():
    rm, sm, n = _make()
    rm._maybe_sweep_profit()
    _set(sm, 1003.0)
    rm._maybe_sweep_profit()
    assert sm.get_risk_state()["trading_balance"] == 1003.0 and not n.sent


def test_zero_percent_is_pure_compounding_and_100_pays_all():
    rm, sm, n = _make(payout_pct=0)
    rm._maybe_sweep_profit()
    _set(sm, 1100.0)
    rm._maybe_sweep_profit()
    rs = sm.get_risk_state()
    assert rs["trading_balance"] == 1100.0 and rs["payout_baseline"] == 1100.0 and not n.sent
    rm, sm, n = _make(payout_pct=100)
    rm._maybe_sweep_profit()
    _set(sm, 1100.0)
    rm._maybe_sweep_profit()
    assert sm.get_risk_state()["trading_balance"] == 1000.0


def test_legacy_threshold_policy_unchanged():
    rm, sm, n = _make(profit_policy="threshold")
    _set(sm, 600.0)
    rm._maybe_sweep_profit()
    assert sm.get_risk_state()["trading_balance"] == 200
