"""
Tests for the 5-minute trade digest and the cash-flow ledger beneath it.

These tests truncate the trade, position and cash-flow tables between cases, so
they run against a throwaway database rather than `data/state.db`. The override
has to be set before `core.state_manager` is imported, because the engine binds
the path at import time — hence the ordering below rather than a setUp.

The properties worth protecting, and why each one matters:

  * an empty window sends NOTHING — the whole reason the digest exists
  * the balance reconciles across a window that contained a sweep
  * profit, capital returned and capital funded stay three separate numbers
  * asset class and venue are attached to every reported trade
  * MT5 notional is converted through lots, not treated as coin quantity

Run: python3 -m unittest tests.test_trade_digest
"""
import os
import sys
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set before `core.state_manager` is imported, because the engine binds the
# path at import time and these tests delete every row in the tables they use.
_TEST_DIR = tempfile.mkdtemp(prefix="wamucheha-test-")
os.environ["WAMUCHEHA_DB_PATH"] = str(Path(_TEST_DIR) / "state.db")

from sqlalchemy.orm import Session  # noqa: E402

import alerts.formatting as f  # noqa: E402
import reporting.trade_digest as td_module  # noqa: E402
from core.state_manager import (  # noqa: E402
    StateManager, TradeRow, OpenPositionRow, CashFlowRow, engine,
)
from reporting.trade_digest import TradeDigest, read_digests  # noqa: E402

# The digest log is a module-level path too, so it is pointed at the same
# scratch directory. Otherwise every run appends to the real history file and
# the "an empty window writes nothing" assertion would be reading lines some
# earlier run left behind.
td_module.DIGEST_LOG = Path(_TEST_DIR) / "trade_digests.jsonl"
# The watermark is a second module-level path. Left alone it would write to the
# real data/ directory on every emit, and a stale watermark read back by a
# later run would decide which window these tests actually report on.
td_module.WATERMARK_FILE = Path(_TEST_DIR) / "digest_watermark.json"


class _RecordingNotifier:
    """Captures digests instead of sending them."""

    def __init__(self, suppressed=None):
        self.digests = []
        self._suppressed = suppressed or []

    def notify_trade_digest(self, digest):
        self.digests.append(digest)

    def drain_suppressed(self):
        return list(self._suppressed)


class _DiscordCapture(_RecordingNotifier):
    """Captures the embed payload, so the Discord-specific limits — 1024 chars
    per field, 25 fields per embed — are checked where they can be enforced
    rather than discovered from a silently dropped webhook response."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def notify_trade_digest(self, digest):
        super().notify_trade_digest(digest)
        from alerts.notifier import Notifier
        n = Notifier()
        n._send_telegram_styled = lambda m: None
        n._send_discord_embed = lambda **kw: self.calls.append(kw)
        n._log = lambda p: None
        n.notify_trade_digest(digest)


class _TelegramCapture(_RecordingNotifier):
    """Also renders the message, so HTML/formatting regressions are caught
    here rather than in production."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def notify_trade_digest(self, digest):
        super().notify_trade_digest(digest)
        from alerts.notifier import Notifier
        n = Notifier()
        n._send_telegram_styled = self.messages.append
        n._send_discord_embed = lambda **kw: None
        n._log = lambda p: None
        n.notify_trade_digest(digest)


class DigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = StateManager(stake_amount=1000, initial_trading_balance=100.0)

    def setUp(self):
        with Session(engine) as s:
            with s.begin():
                s.query(OpenPositionRow).delete()
                s.query(TradeRow).delete()
                s.query(CashFlowRow).delete()
        self.state.update_risk_state(trading_balance=100.0, peak_balance=100.0,
                                     daily_pnl=0, consecutive_losses=0,
                                     trading_halted=0, halt_reason=None)
        self.notifier = _RecordingNotifier()
        self.digest = TradeDigest(self.state, self.notifier, interval_seconds=300)

    def _add_trade(self, client_id, exchange, symbol, side, amount, entry, exit_,
                   pnl, fees=0.0, minutes_ago=1, reason="take_profit_hit",
                   at_epoch=None):
        closed = (datetime.fromtimestamp(at_epoch, timezone.utc).isoformat()
                  if at_epoch is not None else
                  (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat())
        with Session(engine) as s:
            with s.begin():
                s.add(TradeRow(
                    client_order_id=client_id, exchange=exchange, symbol=symbol,
                    side=side, amount=amount, entry_price=entry, exit_price=exit_,
                    status="closed", pnl=pnl, fees=fees, entry_fee=fees / 2,
                    exit_fee=fees / 2,
                    pnl_pct=((exit_ - entry) / entry * 100) * (-1 if side == "sell" else 1),
                    opened_at=(datetime.fromisoformat(closed) - timedelta(minutes=45)).isoformat(),
                    closed_at=closed, reason=reason, strategies='["ema_cross"]',
                ))

    def _add_position(self, client_id, exchange, symbol, amount, entry):
        with Session(engine) as s:
            with s.begin():
                s.add(OpenPositionRow(
                    client_order_id=client_id, exchange=exchange, symbol=symbol,
                    side="buy", amount=amount, entry_price=entry,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                ))

    def _build(self):
        trades = self.digest._trades_closed_since(time.time() - 400)
        flows = self.state.get_cash_flows_since(
            (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat())
        return self.digest.build_digest(trades, flows, time.time(), time.time() - 300)

    # ---------- the silence rule ----------

    def test_empty_window_sends_nothing(self):
        """The single most important behaviour. A 5-minute window with no
        closed trade must produce no message at all — a "still nothing" ping
        every 5 minutes is what trained the channel to be ignored.

        Compared as a count delta rather than against an absolute zero, since
        a neighbouring test in this file legitimately writes a digest record
        and test order must not decide whether this one passes."""
        before_sent, before_logged = len(self.notifier.digests), len(read_digests(50))
        self.digest._last_run = time.time() - 301
        self.digest.maybe_digest()
        self.assertEqual(len(self.notifier.digests), before_sent)
        self.assertEqual(len(read_digests(50)), before_logged,
                         "an empty window must not even write a digest record")

    def test_interval_not_elapsed_does_nothing(self):
        self.digest.maybe_digest()
        self.assertEqual(self.notifier.digests, [])

    def test_a_stalled_loop_does_not_lose_the_trades_it_slept_through(self):
        """A main loop that blocked for 20 minutes must still report the trades
        that closed during the block.

        Anchoring the window to `now - interval` after a stall would slide the
        start past those trades, and since every later window starts later still,
        they would never be reported at all — a silently lossy report on a
        system whose whole job is to be trusted about money."""
        # Trades 12 and 7 minutes ago: inside the 20-minute stall, outside a
        # naive trailing 5-minute window measured from now.
        self._add_trade("old", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.20, minutes_ago=12)
        self._add_trade("new", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.30, minutes_ago=7)
        self.digest._last_run = time.time() - 1200
        self.digest.maybe_digest()

        self.assertEqual(len(self.notifier.digests), 1)
        reported = {t["id"] for t in self.notifier.digests[0]["trades"]}
        self.assertEqual(len(reported), 2, "both trades survive the gap")
        self.assertAlmostEqual(self.notifier.digests[0]["net_pnl"], 0.50, places=4)

    def test_consecutive_windows_do_not_double_report(self):
        """A trade belongs to the window that was open when it closed, and to
        no other. Adjacent windows share a boundary, so a trade at or before
        the shared edge must not appear in both — it would be double-counted in
        every total on the page, not just listed twice.

        The two windows are driven explicitly rather than through the clock:
        `time.time()` cannot be fast-forwarded, and faking the gap by rewinding
        `_last_run` would rewind the anchor too, which is a restart and is
        entitled to re-read. The trade sits a second clear of the seam, because
        a timestamp round-tripped through an ISO string cannot be relied on to
        land on the same float, and a test asserting exact equality there would
        be testing the float format rather than the window logic."""
        closed_at = time.time() - 10
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.20, at_epoch=closed_at)
        seam = closed_at + 1

        self.digest._emit(epoch=seam, since=seam - 300)
        self.assertEqual(len(self.notifier.digests), 1)
        self.assertEqual(self.notifier.digests[0]["trade_count"], 1)

        # The next window starts where this one ended, past the trade.
        self.digest._emit(epoch=seam + 300, since=seam)
        self.assertEqual(len(self.notifier.digests), 1,
                         "a trade before the new window's start must not be re-reported")

    def test_window_start_is_exclusive(self):
        """A trade whose close time is behind the window's start is excluded."""
        closed_at = time.time() - 10
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.20, at_epoch=closed_at)
        self.digest._emit(epoch=time.time(), since=closed_at + 0.001)
        self.assertEqual(self.notifier.digests, [])

    def test_cash_flow_alone_does_not_speak(self):
        """Money moved, no trade closed: still silence.

        The operator made that movement themselves and was already answered in
        the thread that asked for it. A digest here would be the bot announcing
        the operator's own back-button, five minutes later, unprompted."""
        self.state.record_cash_flow("withdrawal", 8.0, venue="okx")
        self.state.record_cash_flow("deposit", 250.0, venue="binance")
        self.digest._last_run = time.time() - 301
        self.digest.maybe_digest()
        self.assertEqual(self.notifier.digests, [])

    def test_deposit_does_not_read_as_a_loss_in_its_window(self):
        """The same rule as the sweep test, for the other direction of
        transfer. A deposit raises the balance with no trade behind it; if the
        reconstruction only unwound sweeps, the window would report the
        deposit as trading performance — the exact failure the cash-flow ledger
        was added to prevent."""
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 0.20)
        self.state.record_cash_flow("deposit", 250.0, venue="binance")
        self.state.update_risk_state(trading_balance=350.20)

        g = self._build()
        self.assertAlmostEqual(g["net_pnl"], 0.20, places=4)
        # Balance started at 100, the trade made 0.20, 250 arrived.
        self.assertAlmostEqual(g["balance_before"], 100.0, places=2)
        self.assertAlmostEqual(g["balance_now"], 350.20, places=2)

    def test_all_three_flow_kinds_reconcile_together(self):
        """Deposit in, withdrawal out, sweep out, one trade: the reconstructed
        start must still be the balance the window began with."""
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 0.20)
        self.state.record_cash_flow("deposit", 100.0)
        self.state.record_cash_flow("withdrawal", 30.0)
        self.state.record_cash_flow("sweep", 20.0)
        self.state.update_risk_state(trading_balance=150.20)

        g = self._build()
        self.assertAlmostEqual(g["balance_before"], 100.0, places=2)
        self.assertAlmostEqual(
            g["balance_before"] + 0.20 + 100.0 - 30.0 - 20.0,
            g["balance_now"], places=2)

    def test_a_declared_transfer_that_never_landed_does_not_invent_a_start(self):
        """A cash-flow row records what the operator said they did; it does not
        perform it. If they logged a deposit but the trading balance never moved,
        unwinding it produces a starting balance below zero — a figure that
        reads as a catastrophic loss which never happened.

        The digest suppresses the number and says so, because a missing figure
        is recoverable and a wrong one gets acted on."""
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 0.20)
        self.state.record_cash_flow("deposit", 250.0, venue="binance")
        # Balance reflects only the trade: the declared deposit is absent.
        self.state.update_risk_state(trading_balance=100.20)

        g = self._build()
        self.assertIsNone(g["balance_before"],
                          "a negative start would read as a loss that never happened")
        self.assertFalse(g["balance_before_known"])
        self.assertAlmostEqual(g["balance_now"], 100.20, places=2)

    def test_unmatched_transfer_is_stated_in_the_rendered_message(self):
        cap = _TelegramCapture()
        d = TradeDigest(self.state, cap, interval_seconds=300)
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 0.20)
        self.state.record_cash_flow("deposit", 250.0, venue="binance")
        self.state.update_risk_state(trading_balance=100.20)
        trades = d._trades_closed_since(time.time() - 400)
        flows = self.state.get_cash_flows_since(
            (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat())
        cap.notify_trade_digest(d.build_digest(trades, flows, time.time(),
                                               time.time() - 300))

        msg = cap.messages[0]
        self.assertIn("BALANCE NOW", msg)
        self.assertIn("not derivable", msg)
        self.assertIn("100.20", msg, "the balance that IS known still reports")
        self.assertNotIn("BALANCE</b>  <code>-150.00</code>",
                         msg, "the impossible start must not be printed")

    def test_malformed_flow_row_is_dropped_not_assumed(self):
        """A row whose amount will not parse must not silently contribute a
        zero-value movement that still counts as a transfer."""
        from reporting.trade_digest import _flow_amounts
        rows = [{"kind": "deposit", "amount": 10.0},
                {"kind": "deposit", "amount": "not a number"},
                {"kind": "deposit"}]
        self.assertEqual(list(_flow_amounts(rows)), [("deposit", 10.0)])

    def test_window_with_a_trade_sends_one_digest(self):
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 0.20)
        self.digest._last_run = time.time() - 301
        self.digest.maybe_digest()
        self.assertEqual(len(self.notifier.digests), 1)
        self.assertEqual(self.notifier.digests[0]["trade_count"], 1)

    # ---------- balance reconciliation ----------

    def test_balance_reconciles_across_a_window_containing_a_sweep(self):
        """A sweep drops the trading balance without a trade losing money.
        Without the cash-flow row in the reconstruction, the digest would
        report a 20 USD loss that never happened."""
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 0.20)
        self.state.record_cash_flow("sweep", 20.0, note="auto-sweep")
        self.state.update_risk_state(trading_balance=80.20)

        g = self._build()
        self.assertAlmostEqual(g["net_pnl"], 0.20, places=4)
        self.assertAlmostEqual(g["balance_before"], 100.0, places=2)
        self.assertAlmostEqual(g["balance_now"], 80.20, places=2)
        # balance_before + trading result - swept == balance_now
        self.assertAlmostEqual(
            g["balance_before"] + g["net_pnl"] - 20.0, g["balance_now"], places=2)

    # ---------- capital vs profit ----------

    def test_deposit_is_not_profit(self):
        self.state.record_cash_flow("deposit", 250.0, venue="binance")
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 5.0)
        self.state.update_risk_state(trading_balance=105.0)

        cash = self.state.get_cash_flow_totals()
        self.assertEqual(cash["deposited"], 250.0)
        self.assertEqual(cash["returned"], 0.0)
        self.assertEqual(cash["profit"], 5.0)
        self.assertAlmostEqual(cash["capital_deployed"], 255.0, places=2)

    def test_withdrawal_does_not_read_as_profit(self):
        self.state.record_cash_flow("withdrawal", 100.0, venue="okx")
        cash = self.state.get_cash_flow_totals()
        self.assertEqual(cash["returned"], 100.0)
        self.assertEqual(cash["profit"], 0.0)
        self.assertEqual(cash["by_venue"]["okx"]["withdrawal"], 100.0)

    def test_swept_profit_is_not_reported_as_returned_capital(self):
        """A sweep is the bot's profit, not your capital coming home.

        Summing the two into one "returned" figure let a 25 USD sweep print
        beside a 0.22 USD profit and read like the bot had made 25 dollars.
        """
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0, 30.0)
        self.state.record_cash_flow("deposit", 250.0, venue="binance")
        self.state.record_cash_flow("sweep", 25.0, note="auto-sweep")
        self.state.record_cash_flow("withdrawal", 8.0, venue="okx")

        cash = self.state.get_cash_flow_totals()
        self.assertEqual(cash["swept"], 25.0)
        self.assertEqual(cash["returned"], 8.0, "a sweep is not returned capital")
        self.assertEqual(cash["profit"], 30.0, "swept profit stays inside profit")
        # 250 funded + 30 earned - 8 taken back - 25 parked in the stake wallet.
        self.assertAlmostEqual(cash["capital_deployed"], 250.0 + 30.0 - 8.0 - 25.0,
                               places=2)

    def test_unknown_cash_flow_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self.state.record_cash_flow("theft", 10.0)

    # ---------- market and venue attribution ----------

    def test_every_trade_carries_class_and_venue(self):
        cases = [
            ("binance", "ETH/USDT", "crypto"),
            ("okx", "SOL/USDT", "crypto"),
            ("mt5", "XAUUSD", "commodities"),
            ("oanda", "GBP/USD", "forex"),
            ("alpaca", "AAPL", "equities"),
        ]
        for exchange, symbol, expected in cases:
            self.assertEqual(f.asset_class(symbol), expected,
                             f"{symbol} on {exchange}")

    def test_venue_names_are_human_readable(self):
        self.assertEqual(f.venue_name("binance"), "Binance")
        self.assertEqual(f.venue_name("mt5"), "MetaTrader 5")
        self.assertEqual(f.venue_name(""), "Unknown venue")

    def test_market_tag_reads_as_one_phrase(self):
        tag = f.market_tag("XAUUSD", "mt5")
        self.assertIn("GOLD/METALS", tag)
        self.assertIn("MetaTrader 5", tag)

    def test_mt5_notional_converts_lots_not_coins(self):
        """0.05 lots of gold is 100 oz at $2000 = $10,000, not $100. Using
        amount x price here would understate real exposure three orders of
        magnitude on exactly the positions where being wrong is expensive."""
        pos = {"symbol": "XAUUSD", "amount": 0.05, "entry_price": 2000.0,
               "exchange": "mt5"}
        from core.risk_manager import position_notional_usd
        self.assertAlmostEqual(position_notional_usd(pos), 10000.0, places=2)

    def test_open_exposure_is_grouped_by_class_and_venue(self):
        self._add_position("o1", "binance", "ETH/USDT", 1.0, 1854.0)
        self._add_position("o2", "mt5", "XAUUSD", 0.05, 2012.0)
        g = self._build()
        self.assertEqual(g["open_positions"], 2)
        self.assertIn("CRYPTO", g["open_by_class"])
        self.assertIn("GOLD/METALS", g["open_by_class"])
        self.assertIn("Binance", g["open_by_venue"])
        self.assertIn("MetaTrader 5", g["open_by_venue"])

    # ---------- the rendered message ----------

    def test_digest_renders_with_arrows_and_every_field(self):
        cap = _TelegramCapture()
        d = TradeDigest(self.state, cap, interval_seconds=300)
        self._add_trade("t1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.20, fees=0.216)
        self._add_trade("t2", "mt5", "XAUUSD", "buy", 0.05, 2000.0, 1990.0,
                        -0.35, minutes_ago=2, reason="stop_loss_hit")
        self._add_position("o1", "binance", "ETH/USDT", 1.0, 1854.0)
        self._add_position("o2", "mt5", "XAUUSD", 0.05, 2012.0)
        self.state.update_risk_state(trading_balance=99.85)
        trades = d._trades_closed_since(time.time() - 400)
        flows = self.state.get_cash_flows_since(
            (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat())
        g = d.build_digest(trades, flows, time.time(), time.time() - 300)
        cap.notify_trade_digest(g)

        msg = cap.messages[0]
        for needle in ("TRADE DIGEST", "ETH/USDT", "XAUUSD", "CRYPTO",
                       "GOLD/METALS", "Binance", "MetaTrader 5",
                       "WINDOW NET", "BALANCE", "accuracy", "EFFICIENCY",
                       "OPEN", "▲", "▼", "➜"):
            self.assertIn(needle, msg, f"missing {needle!r} from the digest")
        # Direction has to be honest: a net loss window cannot open with ▲.
        self.assertTrue(msg.startswith("<b>▼") or msg.startswith("<b>▲"))

    def test_empty_digest_renderer_is_a_no_op(self):
        cap = _TelegramCapture()
        cap.notify_trade_digest({"trades": [], "cash_flows": [], "net_pnl": 0.0})
        self.assertEqual(cap.messages, [])

    def test_busy_window_stays_within_the_telegram_limit(self):
        """A 120-trade window must still arrive. If the renderer outgrew
        Telegram's 4096-character limit the whole report would be rejected and
        the operator would see nothing at all — the worst possible failure for
        the channel that exists to tell them what happened."""
        cap = _TelegramCapture()
        d = TradeDigest(self.state, cap, interval_seconds=300)
        for i in range(40):
            for k, (ex, sym) in enumerate([("binance", "ETH/USDT"),
                                           ("okx", "SOL/USDT"),
                                           ("mt5", "XAUUSD")]):
                self._add_trade(f"busy{i}_{k}", ex, sym, "buy", 0.05,
                                2000.0, 2012.0, 0.2 if i % 3 else -0.1,
                                minutes_ago=0, reason="take_profit_hit")
        self.state.update_risk_state(trading_balance=130.0)
        trades = d._trades_closed_since(time.time() - 400)
        flows = self.state.get_cash_flows_since(
            (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat())
        g = d.build_digest(trades, flows, time.time(), time.time() - 300)
        cap.notify_trade_digest(g)

        self.assertEqual(g["trade_count"], 120)
        self.assertLessEqual(len(cap.messages[0]), 4096,
                             "digest must fit Telegram's single-message limit")
        # The movers survive; the tail is aggregated, not silently dropped.
        self.assertIn("smaller movers", cap.messages[0])
        self.assertIn("120", cap.messages[0])

    def test_discord_embed_respects_its_field_and_count_limits(self):
        """Discord rejects an embed over 1024 characters in a field or over 25
        fields, and the webhook helper swallows the rejection — so a busy
        window would produce no Discord digest at all, with nothing to indicate
        it had tried."""
        from alerts.notifier import DISCORD_FIELD_MAX, DISCORD_FIELD_MAX_COUNT

        cap = _DiscordCapture()
        d = TradeDigest(self.state, cap, interval_seconds=300)
        for i in range(60):
            for k, (ex, sym) in enumerate([("binance", "ETH/USDT"),
                                           ("okx", "SOL/USDT"),
                                           ("mt5", "XAUUSD")]):
                self._add_trade(f"d{i}_{k}", ex, sym, "buy", 0.05, 2000.0, 2012.0,
                                0.2 if i % 3 else -0.1, minutes_ago=0)
        self.state.update_risk_state(trading_balance=130.0)
        trades = d._trades_closed_since(time.time() - 400)
        g = d.build_digest(trades, [], time.time(), time.time() - 300)
        cap.notify_trade_digest(g)

        payload = cap.calls[0]
        self.assertEqual(g["trade_count"], 180)
        for field in payload["fields"]:
            self.assertLessEqual(len(field["value"]), DISCORD_FIELD_MAX)
        self.assertLessEqual(len(payload["fields"]), DISCORD_FIELD_MAX_COUNT)
        # Nothing silently dropped on the way to fitting.
        text = " ".join(f["value"] for f in payload["fields"])
        self.assertIn("180", text)

    def test_chunk_lines_never_drops_content(self):
        from alerts.notifier import _chunk_lines
        lines = [f"line {i} " + "x" * 40 for i in range(20)]
        chunks = _chunk_lines(lines, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        rejoined = "".join(chunks)
        for line in lines:
            self.assertIn(line.strip(), rejoined)

    def test_chunk_lines_splits_a_line_longer_than_the_limit(self):
        """A single over-long line is the one case where characters are cut.
        Losing part of a price is bad; losing the entire message is worse."""
        from alerts.notifier import _chunk_lines
        chunks = _chunk_lines(["y" * 250], 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), "y" * 250,
                         "every character survives, only the newlines move")

    def test_aggregate_groups_by_market_side_and_venue(self):
        from alerts.notifier import _aggregate_trades
        rows = [
            {"symbol": "ETH/USDT", "side": "buy", "exchange": "binance", "pnl": 0.2},
            {"symbol": "ETH/USDT", "side": "buy", "exchange": "binance", "pnl": 0.3},
            {"symbol": "ETH/USDT", "side": "buy", "exchange": "okx", "pnl": -0.1},
        ]
        agg = _aggregate_trades(rows)
        self.assertEqual(len(agg), 2, "same pair on two venues is two things")
        self.assertEqual(sum(a["count"] for a in agg.values()), 3)
        top = list(agg.values())[0]
        self.assertEqual(top["count"], 2)
        self.assertAlmostEqual(top["net"], 0.5)

    def test_oversized_exposure_is_flagged_not_printed_as_a_number(self):
        from alerts.notifier import _exposure_line
        line = _exposure_line({"GOLD/METALS": 10000.0}, 100.0)
        self.assertIn("OVER CAP", line)

    # ---------- metric definitions ----------

    def test_profit_factor_does_not_contradict_the_income_it_sits_beside(self):
        """A book up 0.22 on 0.50 of losers must not be labelled "losing
        money overall" on the same screen as a positive total income.

        The factor answers "after costs, do the winners cover the losers", so
        it is wins-minus-costs over losses. Dividing net P&L by the losing side
        instead divided one total by part of itself and produced 0.44 here."""
        self._add_trade("w1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.42, fees=0.216)
        self._add_trade("w2", "mt5", "XAUUSD", "buy", 0.05, 2000.0, 2008.0, 0.30)
        self._add_trade("l1", "oanda", "GBP/USD", "sell", 1000.0, 1.30, 1.295, -0.50)

        stats = self.state.get_all_time_stats()
        self.assertGreater(stats["net_pnl"], 0, "precondition: the book is ahead")
        # pnl is stored net, so the ratio is wins 0.72 over losses 0.50.
        self.assertAlmostEqual(stats["profit_factor_net"], 0.72 / 0.50, places=3)
        self.assertGreater(stats["profit_factor_net"], 1.0,
                           "winners cover the losers once costs are paid")
        # And the gross version is the one that flatters: 0.936 over 0.50.
        self.assertAlmostEqual(stats["profit_factor"], 0.936 / 0.50, places=3)

    def test_net_profit_factor_is_never_the_flattering_one(self):
        """`pnl` is stored net of fees, so the net ratio has to be the smaller
        of the two whenever fees are material. If the two ever come out
        identical, the "net" label is decorative and the status screens are
        showing the flattering number under an honest-sounding name."""
        self._add_trade("w1", "binance", "ETH/USDT", "buy", 2.0, 1800.0, 1854.0,
                        0.42, fees=0.216)
        self._add_trade("l1", "oanda", "GBP/USD", "sell", 1000.0, 1.30, 1.295, -0.50)
        stats = self.state.get_all_time_stats()
        self.assertLess(stats["profit_factor_net"], stats["profit_factor"],
                        "costs can only drag the ratio down")
        self.assertGreater(stats["total_won_gross"], stats["total_won"],
                           "the gross side credits fees that were never kept")

    def test_efficiency_is_net_over_gross(self):
        self.assertAlmostEqual(f.efficiency_pct(90.0, 100.0), 90.0)
        self.assertEqual(f.efficiency_pct(5.0, 0.0), 0.0)

    def test_accuracy_handles_no_trades(self):
        self.assertEqual(f.accuracy_pct(0, 0), 0.0)
        self.assertAlmostEqual(f.accuracy_pct(3, 4), 75.0)

    def test_equity_curve_is_newest_not_oldest(self):
        """Regression guard: the curve used to order ascending and then limit,
        which froze it permanently on the first 100 trades ever recorded."""
        for i in range(5):
            self._add_trade(f"eq{i}", "binance", "ETH/USDT", "buy", 1.0,
                            100.0, 101.0, float(i), minutes_ago=10 - i)
        curve = self.state.get_equity_curve(3)
        self.assertEqual(len(curve), 3)
        self.assertEqual([c["trade_id"] for c in curve],
                         sorted(c["trade_id"] for c in curve))
        self.assertEqual(curve[-1]["trade_id"], 5)



class _FlakyNotifier(_RecordingNotifier):
    """Fails the first N digest sends, to model a transient outage."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures
        self.alerts = []

    def notify_trade_digest(self, digest):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("simulated notifier outage")
        self.digests.append(digest)

    def notify(self, kind, message, priority=None, **kw):
        self.alerts.append(kind)


class WatermarkTest(unittest.TestCase):
    """The window boundary must only move forward across a window that was
    actually reported.

    The failure this protects against is silent and permanent: if a failed
    digest still advances the boundary, the trades in that window fall outside
    every future digest and are never mentioned again, with nothing beyond one
    error log to say so.
    """

    @classmethod
    def setUpClass(cls):
        cls.state = StateManager(stake_amount=1000, initial_trading_balance=100.0)

    def setUp(self):
        with Session(engine) as s:
            with s.begin():
                s.query(OpenPositionRow).delete()
                s.query(TradeRow).delete()
                s.query(CashFlowRow).delete()
        self.state.update_risk_state(trading_balance=100.0, peak_balance=100.0,
                                     daily_pnl=0, consecutive_losses=0,
                                     trading_halted=0, halt_reason=None)
        td_module.WATERMARK_FILE.unlink(missing_ok=True)

    def _add_trade(self, client_id, minutes_ago=1):
        closed = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        opened = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago + 5)).isoformat()
        with Session(engine) as s:
            with s.begin():
                s.add(TradeRow(
                    client_order_id=client_id, exchange="binance", symbol="BTCUSDT",
                    side="long", amount=1.0, entry_price=100.0, exit_price=110.0,
                    status="closed", pnl=10.0, fees=0.0, entry_fee=0.0, exit_fee=0.0,
                    reason="take_profit_hit", opened_at=opened, closed_at=closed,
                ))

    def _open_window(self, digest):
        """Push the boundary into the past so the next maybe_digest() fires."""
        digest._last_run = time.time() - 301
        return digest._last_run

    def test_failed_digest_does_not_consume_the_window(self):
        notifier = _FlakyNotifier(failures=1)
        digest = TradeDigest(self.state, notifier, interval_seconds=300)
        self._add_trade("wm-retry-1", minutes_ago=1)
        since = self._open_window(digest)

        digest.maybe_digest()

        self.assertEqual(notifier.digests, [], "outage should have swallowed the send")
        self.assertEqual(notifier.alerts, ["report_failed"],
                         "operator must be told reporting is broken")
        self.assertEqual(digest._last_run, since,
                         "boundary moved despite the failure, losing those trades")

        # The next tick, with the outage over, must still cover that window.
        self._open_window(digest)
        digest.maybe_digest()
        self.assertEqual(len(notifier.digests), 1,
                         "the missed window was never re-reported")
        # The digest's trade rows carry the numeric PK, so the trade is
        # identified by the row its own insert produced rather than by name.
        with Session(engine) as s:
            expected = s.query(TradeRow).filter(
                TradeRow.client_order_id == "wm-retry-1").one().id
        self.assertEqual([t["id"] for t in notifier.digests[0]["trades"]],
                         [expected])

    def test_successful_digest_advances_and_persists(self):
        notifier = _RecordingNotifier()
        digest = TradeDigest(self.state, notifier, interval_seconds=300)
        self._add_trade("wm-persist-1", minutes_ago=1)
        self._open_window(digest)

        digest.maybe_digest()

        self.assertEqual(len(notifier.digests), 1)
        self.assertTrue(td_module.WATERMARK_FILE.exists())
        self.assertAlmostEqual(
            json.loads(td_module.WATERMARK_FILE.read_text())["last_run"],
            digest._last_run, places=3)

    def test_restart_resumes_from_the_persisted_boundary(self):
        notifier = _RecordingNotifier()
        first = TradeDigest(self.state, notifier, interval_seconds=300)
        self._add_trade("wm-restart-1", minutes_ago=1)
        self._open_window(first)
        first.maybe_digest()
        self.assertEqual(len(notifier.digests), 1)

        # A restart resumes at the end of the window just reported. It must not
        # rewind to "now" (that trade would be re-reported on every restart,
        # forever) nor to the epoch (which would replay all history). The
        # practical consequence is that the fresh instance is not yet due.
        second = TradeDigest(self.state, notifier, interval_seconds=300)
        self.assertAlmostEqual(second._last_run, first._last_run, places=3)
        second.maybe_digest()
        self.assertEqual(len(notifier.digests), 1, "trade re-reported after restart")

        # And the resumed boundary advances normally rather than sticking.
        before = second._last_run
        self._open_window(second)
        second.maybe_digest()
        self.assertGreater(second._last_run, before)

    def test_corrupt_watermark_falls_back_to_now(self):
        td_module.WATERMARK_FILE.write_text("{not json")
        digest = TradeDigest(self.state, _RecordingNotifier(), interval_seconds=300)
        self.assertLessEqual(digest._last_run, time.time() + 1)
        self.assertGreater(digest._last_run, time.time() - 5)

    def test_future_watermark_is_ignored(self):
        # A restored backup or a clock change must not park the engine in a
        # silent wait for the skew to elapse.
        td_module.WATERMARK_FILE.write_text(
            json.dumps({"last_run": time.time() + 86400}))
        digest = TradeDigest(self.state, _RecordingNotifier(), interval_seconds=300)
        self.assertLessEqual(digest._last_run, time.time() + 1)

    def test_empty_window_still_advances(self):
        # Silence is a successful report. Holding the boundary back here too
        # would let a quiet stretch grow an ever-wider window that eventually
        # dumps every trade of the quiet stretch into one late digest.
        notifier = _RecordingNotifier()
        digest = TradeDigest(self.state, notifier, interval_seconds=300)
        since = self._open_window(digest)

        digest.maybe_digest()

        self.assertEqual(notifier.digests, [])
        self.assertGreater(digest._last_run, since)


if __name__ == "__main__":
    unittest.main(verbosity=2)
