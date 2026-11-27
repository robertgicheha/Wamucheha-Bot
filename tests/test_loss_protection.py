"""Broker-side closes are recorded from deal history, and losing streaks pause entries."""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

os.environ.setdefault("WAMUCHEHA_DB_PATH",
                      str(Path(tempfile.mkdtemp(prefix="wamucheha-loss-")) / "state.db"))

import pytest  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core import mt5_executor  # noqa: E402
from core.risk_manager import RiskManager  # noqa: E402
from core.state_manager import StateManager, OpenPositionRow, TradeRow, engine  # noqa: E402

CFG = {"account": {"stake_amount": 1.0},
       "risk": {"max_consecutive_losses": 8, "loss_cooldown_after": 3,
                "loss_cooldown_minutes": 240, "max_daily_loss_pct": 5,
                "max_position_pct": 3, "profit_policy": "split"}}
ID = "bot-mt5-XAUUSD-abc123abc123"


class _Quiet:
    def __getattr__(self, _):
        return lambda *a, **k: None


@pytest.fixture
def state():
    st = StateManager(stake_amount=1.0)
    with Session(engine) as s, s.begin():
        s.query(OpenPositionRow).delete()
        s.query(TradeRow).delete()
    st.update_risk_state(trading_balance=10000.0, peak_balance=10000.0, payout_baseline=10000.0,
                         consecutive_losses=0, daily_pnl=0.0, trading_halted=0, halt_reason=None,
                         daily_reset_at=datetime.now(timezone.utc).date().isoformat())
    return st


def _open(state):
    state.record_trade_open(ID, "mt5", "XAUUSD", "buy", 0.01, 3500.0, 3430.0, 3587.5)


def _deal(entry, price, profit, reason=0, volume=0.01):
    return NS(symbol="XAUUSD", entry=entry, comment=ID[:31] if entry == 0 else "[sl]",
              position_id=7, volume=volume, price=price, profit=profit, swap=0.0,
              commission=-0.05, fee=0.0, reason=reason)


def _executor(state, monkeypatch, positions, deals):
    fake = NS(positions_get=lambda symbol: positions, history_deals_get=lambda a, b: deals,
              last_error=lambda: (1, "x"), symbol_info=lambda s: NS(trade_contract_size=100.0))
    monkeypatch.setattr(mt5_executor, "get_mt5", lambda: fake)
    return mt5_executor.MT5Executor(state, RiskManager(state, CFG, _Quiet()), _Quiet(),
                                    dry_run=False)


def test_broker_stop_is_recorded_and_counted(state, monkeypatch):
    _open(state)
    ex = _executor(state, monkeypatch, (), (_deal(0, 3500.0, 0.0), _deal(1, 3430.0, -70.0, 4)))
    assert ex.sync_broker_closes() == 1
    trade = state.get_recent_trades(1)[0]
    assert trade["reason"] == "stop_loss_hit" and trade["pnl"] == pytest.approx(-70.10)
    assert state.get_risk_state()["consecutive_losses"] == 1
    assert state.get_risk_state()["trading_balance"] == pytest.approx(9929.90)
    assert ex.sync_broker_closes() == 0


@pytest.mark.parametrize("positions", [None, ()])
def test_unconfirmed_close_keeps_the_trade_open(state, monkeypatch, positions):
    """A terminal error, or no closing deal yet, must never be booked as a close."""
    _open(state)
    ex = _executor(state, monkeypatch, positions, (_deal(0, 3500.0, 0.0),))
    assert ex.sync_broker_closes() == 0
    ex.close_trade(ID, 3400.0, "stop_loss_hit")
    assert len(state.get_open_positions()) == 1


def test_open_position_is_left_alone(state, monkeypatch):
    _open(state)
    ex = _executor(state, monkeypatch, (NS(ticket=7, comment=ID[:31], magic=202401),), ())
    assert ex.sync_broker_closes() == 0
    assert len(state.get_open_positions()) == 1


def test_cooldown_blocks_entries_after_three_losses(state):
    risk = RiskManager(state, CFG, _Quiet())
    _open(state)
    state.record_trade_close(ID, 3430.0, -1.0, "stop_loss_hit")
    state.update_risk_state(consecutive_losses=2)
    assert risk.loss_cooldown_remaining() == 0
    state.update_risk_state(consecutive_losses=3)
    assert 0 < risk.loss_cooldown_remaining() <= 240 * 60
    assert not risk.pre_trade_check(100.0).allowed


def test_find_symbol_maps_to_what_the_broker_lists():
    from data_feeds.feed_router import find_symbol
    mt5 = ["XAUUSD", "BTCUSD", "ETHUSD"]
    assert find_symbol("BTC/USDT", mt5) == "BTCUSD"
    assert find_symbol("PAXG/USDT", mt5) == "XAUUSD"
    assert find_symbol("ETH/USDT", ["BTC/USDT", "ETH/USDT"]) == "ETH/USDT"
    assert find_symbol("BTC/USDT", ["XAUUSD"]) is None
