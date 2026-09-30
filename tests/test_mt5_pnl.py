"""
MT5 P&L accounting — the one place where the arithmetic itself was wrong.

The other venue tests assert that a cost model is *wired up*. This one checks
the number, because on MT5 the wiring being present does not make the answer
right: `amount` is in lots, and treating lots as units prices a gold position
100x too small. A token-presence test passes happily through that bug, and the
bug is invisible in a report that also prints the fees correctly.

The invariant worth stating: what the broker reports as realized P&L is what
the balance moves by, and the gap between that and the price move is reported
as cost rather than quietly absorbed.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The state manager binds its database path at import time, so this has to be
# set before the import below. Left alone it would open (and write to) the real
# trading database.
_TEST_DIR = tempfile.mkdtemp(prefix="wamucheha-mt5-")
os.environ["WAMUCHEHA_DB_PATH"] = str(Path(_TEST_DIR) / "state.db")

from core.fee_manager import FeeModel  # noqa: E402
from core.mt5_executor import MT5Executor  # noqa: E402
from core.state_manager import StateManager, OpenPositionRow, TradeRow, engine  # noqa: E402

GOLD_CONTRACT = 100.0  # 1 lot XAUUSD = 100 oz


def _clear_tables():
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        with session.begin():
            session.query(OpenPositionRow).delete()
            session.query(TradeRow).delete()


class _FakeSymbolInfo:
    def __init__(self, contract_size=GOLD_CONTRACT):
        self.trade_contract_size = contract_size


class _FakeMT5:
    """Just enough of the terminal for the close path.

    `broker_profit` is what `order_calc_profit` returns, i.e. the broker's own
    realized figure. None means "the terminal could not answer", which is the
    branch that has to fall back to the modelled number.
    """

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

    def __init__(self, contract_size=GOLD_CONTRACT, broker_profit=None):
        self.contract_size = contract_size
        self.broker_profit = broker_profit
        self.calc_calls = []

    def symbol_info(self, symbol):
        return _FakeSymbolInfo(self.contract_size)

    def order_calc_profit(self, order_type, symbol, volume, entry, exit_):
        self.calc_calls.append((order_type, symbol, volume, entry, exit_))
        return self.broker_profit


class _RecordingNotifier:
    def __init__(self):
        self.opened = []
        self.closed = []

    def notify(self, *a, **kw):
        pass

    def notify_trade_opened(self, **kw):
        self.opened.append(kw)

    def notify_trade_closed(self, **kw):
        self.closed.append(kw)


class _FakeRisk:
    def __init__(self):
        self.closed_with = []

    def pre_trade_check(self, *a, **kw):
        raise AssertionError("close path must not re-run the entry risk check")

    def on_trade_closed(self, pnl):
        self.closed_with.append(pnl)


class MT5PnlAccountingTest(unittest.TestCase):
    def setUp(self):
        # Constructing the manager creates the tables if they are not there yet.
        self.state = StateManager(stake_amount=1000, initial_trading_balance=100_000)
        # Every test in the class shares the one scratch database, and
        # trades.client_order_id is UNIQUE, so leftovers from a previous test
        # would surface as an IntegrityError instead of an assertion failure.
        _clear_tables()
        self.notifier = _RecordingNotifier()
        self.risk = _FakeRisk()
        # MT5 charges no commission, so the cost model prices at 0. The test is
        # about the P&L arithmetic, not the rate.
        self.fees = FeeModel({"enabled": True, "per_venue_taker_bps": {"mt5": 0.0}})

    def _executor(self, mt5):
        executor = MT5Executor(
            state_manager=self.state, risk_manager=self.risk, notifier=self.notifier,
            dry_run=True, fee_model=self.fees,
        )
        return executor, mock.patch("core.mt5_executor.get_mt5", return_value=mt5)

    def _open_gold_lot(self, order_id="mt5-1", lots=0.1, entry=2000.0):
        self.state.record_trade_open(
            order_id, "mt5", "XAUUSD", "buy", lots, entry,
            stop_loss_price=1980.0, take_profit_price=2040.0,
            strategies=["trend"], score=0.8, regime="trending",
        )

    def test_lots_are_not_units(self):
        """0.1 lots of gold moving $10 is $100, not $1.

        This is the regression. Computed as (exit - entry) * amount the answer
        is $1.00, a hundred times too small, and it flows straight into the
        balance, the daily P&L and the circuit breakers.
        """
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=100.0)
        executor, patcher = self._executor(mt5)
        self._open_gold_lot(lots=0.1, entry=2000.0)

        with patcher:
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        closed = self.notifier.closed
        self.assertEqual(len(closed), 1, "close must notify exactly once")
        # gross = 0.1 lot * 100 oz * $10 = $100, which is also the broker's
        # number here, so the cost between them is zero.
        self.assertAlmostEqual(closed[0]["gross_pnl"], 100.0, places=6)
        self.assertAlmostEqual(closed[0]["pnl"], 100.0, places=6)
        self.assertEqual(mt5.calc_calls[0][2], 0.1,
                         "the broker must be asked about the lot size, not the notional")

    def test_a_three_lot_position_scales(self):
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=3000.0)
        executor, patcher = self._executor(mt5)
        self._open_gold_lot(lots=3.0, entry=2000.0)

        with patcher:
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        self.assertAlmostEqual(self.notifier.closed[0]["gross_pnl"], 3000.0, places=6)

    def test_broker_realized_pnl_drives_the_balance(self):
        """When the broker's number disagrees with the modelled one, the
        broker's wins — it is what actually landed in the account — and the
        difference is reported as cost rather than disappearing."""
        # Broker says $90 where the price move says $100: $10 of spread/commission.
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=90.0)
        executor, patcher = self._executor(mt5)
        self._open_gold_lot(lots=0.1, entry=2000.0)

        with patcher:
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        closed = self.notifier.closed[0]
        self.assertAlmostEqual(closed["pnl"], 90.0, places=6,
                               msg="the balance must move by the broker's realized figure")
        self.assertAlmostEqual(closed["exit_fee"], 10.0, places=6)
        self.assertAlmostEqual(closed["gross_pnl"], 100.0, places=6)
        # gross - cost == net, so the report is internally consistent.
        self.assertAlmostEqual(
            closed["gross_pnl"] - closed["entry_fee"] - closed["exit_fee"],
            closed["pnl"], places=6)
        self.assertEqual(self.risk.closed_with, [90.0],
                         "the risk manager must see the same net number")

    def test_a_favourable_fill_is_not_a_negative_fee(self):
        """Broker reports $110 on a $100 move (a better exit than modelled).
        A negative fee is not a thing, and printing one would make the cost
        line lie in the other direction."""
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=110.0)
        executor, patcher = self._executor(mt5)
        self._open_gold_lot(lots=0.1, entry=2000.0)

        with patcher:
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        closed = self.notifier.closed[0]
        self.assertAlmostEqual(closed["exit_fee"], 0.0, places=6)
        self.assertAlmostEqual(closed["pnl"], 110.0, places=6)
        self.assertGreaterEqual(closed["exit_fee"], 0.0)

    def test_falls_back_to_the_modelled_pnl_when_the_terminal_cannot_answer(self):
        """`order_calc_profit` raises on a stale bridge. The close must still
        be recorded — the price move is known — with the cost from the model."""
        class BrokenMT5(_FakeMT5):
            def order_calc_profit(self, *a, **kw):
                raise RuntimeError("bridge timeout")

        executor, patcher = self._executor(BrokenMT5(contract_size=GOLD_CONTRACT))
        self._open_gold_lot(lots=0.1, entry=2000.0)

        with patcher:
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        closed = self.notifier.closed[0]
        self.assertAlmostEqual(closed["pnl"], 100.0, places=6)
        self.assertAlmostEqual(closed["exit_fee"], 0.0, places=6)

    def test_a_configured_commission_is_deducted(self):
        """MT5's default rate is 0, but a broker or an operator who models the
        spread as a cost must have it subtracted rather than ignored."""
        fees = FeeModel({"enabled": True, "per_venue_taker_bps": {"mt5": 20.0}})  # 0.20%
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=None)
        executor = MT5Executor(
            state_manager=self.state, risk_manager=self.risk, notifier=self.notifier,
            dry_run=True, fee_model=fees,
        )
        self._open_gold_lot(lots=0.1, entry=2000.0)

        with mock.patch("core.mt5_executor.get_mt5", return_value=mt5):
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        closed = self.notifier.closed[0]
        notional = 0.1 * GOLD_CONTRACT * 2010.0
        self.assertAlmostEqual(closed["exit_fee"], notional * 0.002, places=6)
        self.assertLess(closed["pnl"], closed["gross_pnl"],
                        "a positive cost must reduce the net")

    def test_hold_time_is_reported(self):
        """`held_seconds` reads opened_at. Without it every close reports a
        zero-minute hold, which makes the digest's timing columns useless."""
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=100.0)
        executor, patcher = self._executor(mt5)
        self._open_gold_lot(lots=0.1, entry=2000.0)

        with patcher:
            executor.close_trade("mt5-1", exit_price=2010.0, reason="target")

        self.assertIsNotNone(self.notifier.closed[0]["opened_at"],
                             "opened_at was not passed, so hold time is always zero")

    def test_short_direction_flips_the_sign(self):
        mt5 = _FakeMT5(contract_size=GOLD_CONTRACT, broker_profit=-100.0)
        executor, patcher = self._executor(mt5)
        self.state.record_trade_open(
            "mt5-s", "mt5", "XAUUSD", "sell", 0.1, 2000.0,
            stop_loss_price=2020.0, take_profit_price=1960.0,
        )

        with patcher:
            executor.close_trade("mt5-s", exit_price=2010.0, reason="stop")

        closed = self.notifier.closed[0]
        self.assertAlmostEqual(closed["gross_pnl"], -100.0, places=6)
        self.assertAlmostEqual(closed["pnl"], -100.0, places=6)

    def test_notional_uses_the_brokers_contract_size(self):
        """Forex majors are 100,000 units a lot; gold is 100. Using the wrong
        one is a 1000x error on the notional that every cost is priced on."""
        mt5 = _FakeMT5(contract_size=100_000.0, broker_profit=None)
        executor, patcher = self._executor(mt5)
        with patcher:
            notional = executor._notional_usd("EURUSD", 0.5, 1.1000)
        self.assertAlmostEqual(notional, 0.5 * 100_000 * 1.1000, places=6)


class MT5NotionalHelpersTest(unittest.TestCase):
    def test_unknown_symbol_falls_back_to_the_standard_contract(self):
        """A symbol the broker has not published must not produce a zero
        notional (which would price every fee at zero) — it falls back to the
        FX standard rather than pretending the position was free."""
        executor = MT5Executor(
            state_manager=mock.Mock(), risk_manager=mock.Mock(), notifier=mock.Mock(),
            dry_run=True,
        )

        class NoInfo:
            def symbol_info(self, symbol):
                return None

        with mock.patch("core.mt5_executor.get_mt5", return_value=NoInfo()):
            notional = executor._notional_usd("MYSTERY", 1.0, 1.0)
        self.assertAlmostEqual(notional, 100_000.0, places=6)

    def test_zero_price_gives_zero_notional_rather_than_dividing(self):
        executor = MT5Executor(
            state_manager=mock.Mock(), risk_manager=mock.Mock(), notifier=mock.Mock(),
            dry_run=True,
        )
        self.assertEqual(executor._notional_usd("XAUUSD", 0.1, 0), 0.0)
        self.assertEqual(executor._notional_usd("XAUUSD", 0, 2000.0), 0.0)


if __name__ == "__main__":
    unittest.main()
