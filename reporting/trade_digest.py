"""
The 5-minute trade digest.

Every trade used to message you the instant it closed. That is the right
instant and the wrong frequency: on a slow strategy it is four identical
"sits idle" messages a day, and on a fast one it is a wall of near-identical
screenshots of the same position being managed. Either way the reader learns
to swipe, and the one message that mattered — a run of losses, a fee problem
— is swiped along with the rest.

So trades are batched. Every 5 minutes, this collects everything that closed
in the window and sends exactly one message about it. If nothing closed,
nothing is sent. That silence is the point: a message with no trade in it can
only ever say "still nothing", and a channel that says that every 5 minutes
is a channel you stop reading.

What a digest has to answer, in this order, because this is the order they
get asked:

  1. What happened?        every trade: market, class, venue, side, prices
  2. Did it make money?    net after costs, and the cost of doing the trade
  3. Where did I end up?   balance before, balance now, and what moved it
  4. Am I ahead?           net, capital returned, and the two kept separate
  5. Is it working?        accuracy, efficiency, expectancy, profit factor
  6. What is exposed?      open positions and the class/venue they sit on

The 6-hour and 24-hour reports still exist and still carry uptime and record.
This does not replace them; it replaces the per-trade flood they were built
to avoid in the first place.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("trade_digest")

DIGEST_LOG = Path(__file__).parent.parent / "data" / "trade_digests.jsonl"

# The window boundary is kept in its own file rather than derived from
# DIGEST_LOG. DIGEST_LOG only gains a line when a digest is actually sent, so
# "no trades" windows leave no trace and a restart would rewind the boundary to
# the last window that had a trade in it, re-reporting old trades forever.
WATERMARK_FILE = Path(__file__).parent.parent / "data" / "digest_watermark.json"


class TradeDigest:
    def __init__(self, state_manager, notifier, interval_seconds: int = 300,
                 risk_manager=None):
        self.state = state_manager
        self.notifier = notifier
        # Held for the venue kill-switch state, which a digest reports when
        # trading is halted part-way through a window.
        self.risk = risk_manager
        self.interval = int(interval_seconds or 300)
        self._last_run = self._load_watermark() or time.time()
        DIGEST_LOG.parent.mkdir(parents=True, exist_ok=True)

    # ---------- watermark persistence ----------

    def _load_watermark(self):
        """Last successfully emitted window boundary, or None.

        A corrupt or missing file is not an error worth raising: the only
        consequence of losing this is a wider window on the first digest after
        a restart, which is recoverable, whereas refusing to start the engine
        over a reporting watermark is not."""
        try:
            value = json.loads(WATERMARK_FILE.read_text())["last_run"]
            value = float(value)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        # A watermark from the future (clock change, restored backup) would
        # make the engine wait out the skew before reporting anything.
        if value > time.time() + 60:
            logger.warning("digest watermark is in the future, ignoring it")
            return None
        return value

    def _save_watermark(self, epoch: float):
        try:
            WATERMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot leave a truncated
            # file that reads back as corrupt and silently rewinds the window.
            tmp = WATERMARK_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"last_run": epoch}))
            tmp.replace(WATERMARK_FILE)
        except OSError as e:
            logger.warning(f"could not persist digest watermark: {e}")


    # ---------- entry point, called once per main loop tick ----------

    def maybe_digest(self):
        now = time.time()
        if now - self._last_run < self.interval:
            return
        # The window is anchored to the last run that actually happened, not to
        # a fixed `now - interval`. When the main loop stalls — a slow scan, a
        # reconnect, the laptop asleep — `now - interval` walks forward past
        # trades that closed in between, and those trades would never appear in
        # any digest. Anchoring to the previous run means a late tick reports a
        # longer window rather than a shorter one, so the only thing a stall can
        # cost is a message covering more time than usual.
        since = self._last_run
        try:
            self._emit(now, since)
        except Exception as e:
            # The window is deliberately NOT advanced on failure. Advancing it
            # here would consume the window: those trades would fall outside
            # every future digest and never be reported at all, which is the
            # exact opposite of what a retry-safe reporter should do. Leaving
            # the boundary alone means the next tick re-reads the same window
            # and the trades get their digest once the fault clears.
            logger.error(f"trade digest failed: {e}")
            try:
                self.notifier.notify(
                    "report_failed",
                    "The 5-minute trade digest could not be built or sent. "
                    "Until this clears, silence from this channel means "
                    "'nothing closed' is NOT guaranteed — check the dashboard. "
                    "No trades are lost: the next successful digest will "
                    "cover the missed window.",
                    priority="high",
                )
            except Exception:
                pass
            return

        # Only now is the window genuinely consumed. `_emit` is a no-op when the
        # window had no trades, and that is still a success — there was simply
        # nothing to report and the boundary should move on.
        self._last_run = now
        self._save_watermark(now)

    def _emit(self, epoch: float, since: float = None):
        if since is None:
            since = epoch - self.interval
        trades = self._trades_closed_since(since)
        flows = self.state.get_cash_flows_since(
            datetime.fromtimestamp(since, timezone.utc).isoformat())
        # No trade: say nothing, even if money moved. This is the single most
        # important behaviour in the file.
        #
        # A window with a withdrawal and no trade is not a reason to speak. The
        # operator made that movement themselves and was already answered in
        # the thread that requested it, so a digest would be the bot announcing
        # the operator's own back-button. Flows are still collected above
        # because a deposit that lands mid-window still has to be backed out of
        # the balance arithmetic of a window that does have a trade in it.
        if not trades:
            return

        digest = self.build_digest(trades, flows, epoch, since)
        self._append(digest)
        self.notifier.notify_trade_digest(digest)

    # ---------- build ----------

    def _trades_closed_since(self, since_epoch: float) -> list:
        """Closed trades in the window, oldest first.

        `closed_at` is an ISO string column, so the window is filtered in
        Python rather than SQL. That is a deliberate trade for now: the
        lexicographic comparison of ISO-8601 UTC strings would let the database
        do the work, but only while every writer agrees on the format and zone,
        and a mixed-offset or naive timestamp would then compare as a real
        date and quietly drop a trade from a report. Reading each row's own
        timestamp and normalising it cannot get that wrong. It costs one scan
        of the closed history every 5 minutes, which at this table's size is
        not the bottleneck — the HTTP posts are."""
        from sqlalchemy.orm import Session
        from core.state_manager import TradeRow, engine

        with Session(engine) as session:
            rows = session.query(TradeRow).filter(
                TradeRow.status == "closed",
                TradeRow.closed_at.isnot(None),
            ).order_by(TradeRow.id.asc()).all()

        out = []
        for r in rows:
            closed = _parse_iso(r.closed_at)
            # The start is exclusive because adjacent windows share it: a trade
            # sitting exactly on that seam would otherwise be reported twice,
            # which double-counts it in every total on the page, not just twice
            # in the list. Consecutive windows are anchored to each other, so
            # the pair still covers the whole timeline with nothing dropped.
            if closed is not None and closed.timestamp() > since_epoch:
                out.append(r)
        return out

    def build_digest(self, trades: list, flows: list, epoch: float,
                     since_epoch: float) -> dict:
        risk_state = self.state.get_risk_state()
        balance_now = float(risk_state.get("trading_balance", 0.0))

        entries = [self._trade_entry(r) for r in trades]
        pnls = [e["pnl"] for e in entries]
        net = sum(pnls)
        gross = sum(e["gross"] for e in entries)
        fees = sum(e["fees"] for e in entries)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)

        # Balance BEFORE the window is reconstructed, not remembered: it is the
        # current balance minus everything that has happened since. Deriving it
        # means a restart mid-window cannot leave a stale anchor.
        #
        # Every kind of cash flow has to be unwound here, not just sweeps. The
        # balance only moves for two reasons — the trades themselves, and money
        # crossing the account boundary — so an unaccounted transfer is read as
        # trading performance. Backing out only sweeps (the earlier version)
        # meant a $250 deposit showed up as a $250 loss in the window it landed
        # in, which is precisely the misreading this line exists to prevent.
        # Sweeps and withdrawals leave, deposits arrive.
        transferred = sum(
            amount if kind == "deposit" else -amount
            for kind, amount in _flow_amounts(flows)
        )
        moved = net + transferred
        balance_before = balance_now - moved

        # The reconstruction assumes every cash-flow row corresponds to a
        # transfer that actually landed in the trading balance. A sweep always
        # does, because the risk manager moves the balance in the same breath.
        # A declared deposit or withdrawal only does if the operator also
        # updated the balance — which is the documented contract of
        # `record_cash_flow`, since the row records what they did, it does not
        # perform it.
        #
        # When they do not line up, the arithmetic still produces a number, and
        # that number is the dangerous part: an account funded with 250 would
        # print a starting balance of -142 and read as though the bot had lost
        # 142 before its first trade. Rather than print a confident wrong
        # figure, the digest falls back to showing the actual movement and says
        # the start could not be established. A missing number is recoverable;
        # a wrong one is acted on.
        flows_unmatched = balance_before < 0 and any(
            kind in ("deposit", "withdrawal") for kind, _ in _flow_amounts(flows))
        if flows_unmatched:
            logger.warning(
                f"digest window has {len(flows)} cash flow(s) that do not match "
                f"the trading balance movement; balance before suppressed "
                f"(reconstructed {balance_before:.2f}, now {balance_now:.2f})")
            balance_before = None

        daily = self.state.get_daily_economics()
        all_time = self.state.get_all_time_stats()
        cash = self.state.get_cash_flow_totals()
        positions = self.state.get_open_positions()
        notional_open = sum(_notional(p) for p in positions)

        return {
            "kind": "digest",
            "epoch": epoch,
            "ts": datetime.now(timezone.utc).isoformat(),
            "window_seconds": self.interval,
            "since_epoch": since_epoch,
            "trades": entries,
            "trade_count": len(entries),
            "wins": wins,
            "losses": losses,
            "net_pnl": round(net, 4),
            "gross_pnl": round(gross, 4),
            "fees": round(fees, 4),
            "best": round(max(pnls), 4) if pnls else 0.0,
            "worst": round(min(pnls), 4) if pnls else 0.0,
            "cash_flows": flows,
            "balance_before": (round(balance_before, 4)
                               if balance_before is not None else None),
            "balance_before_known": balance_before is not None,
            "balance_now": round(balance_now, 4),
            "peak_balance": float(risk_state.get("peak_balance", 0.0)),
            "trading_halted": bool(risk_state.get("trading_halted")),
            "halt_reason": risk_state.get("halt_reason"),
            "consecutive_losses": int(risk_state.get("consecutive_losses", 0) or 0),
            "daily": daily,
            "all_time": all_time,
            "cash": cash,
            "open_positions": len(positions),
            "open_notional": round(notional_open, 4),
            "open_by_class": _group_exposure(positions, "class"),
            "open_by_venue": _group_exposure(positions, "venue"),
            "suppressed": self.notifier.drain_suppressed(),
        }

    @staticmethod
    def _trade_entry(row) -> dict:
        fees = float(row.fees or 0.0)
        pnl = float(row.pnl or 0.0)
        # Notional via the risk manager's converter, not amount x price: on MT5
        # `amount` is lots, so the naive product understates a 0.05-lot gold
        # position by three orders of magnitude and every percentage derived
        # from it with it.
        notional = _notional({
            "symbol": row.symbol, "amount": float(row.amount or 0.0),
            "entry_price": float(row.entry_price or 0.0), "exchange": row.exchange,
        })
        return {
            "id": row.id,
            "symbol": row.symbol,
            "exchange": row.exchange,
            "side": row.side,
            "amount": float(row.amount or 0.0),
            "entry_price": row.entry_price,
            "exit_price": row.exit_price,
            "pnl": pnl,
            "gross": pnl + fees,
            "fees": fees,
            "entry_fee": float(row.entry_fee or 0.0),
            "exit_fee": float(row.exit_fee or 0.0),
            "fees_are_estimated": bool(row.fees_are_estimated),
            "pnl_pct": row.pnl_pct,
            "notional": round(notional, 4),
            "reason": row.reason or "",
            "strategies": json.loads(row.strategies) if row.strategies else [],
            "opened_at": row.opened_at,
            "closed_at": row.closed_at,
            "held_seconds": _held(row.opened_at, row.closed_at),
        }

    @staticmethod
    def _append(digest: dict):
        try:
            with open(DIGEST_LOG, "a") as handle:
                handle.write(json.dumps(digest, default=str) + "\n")
        except Exception as e:
            logger.warning(f"could not write digest log: {e}")


def read_digests(n: int = 20) -> list:
    """Newest first. Module-level so the control bots and the dashboard can
    read the digest history without constructing a reporter (and therefore
    without needing a notifier they would never use to send anything)."""
    if not DIGEST_LOG.exists():
        return []
    out = []
    for line in DIGEST_LOG.read_text().strip().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.sort(key=lambda e: e.get("epoch") or 0, reverse=True)
    return out[:n]


def _flow_amounts(flows: list):
    """(kind, amount) for each cash flow in the window, skipping junk.

    A row with no usable amount is dropped rather than defaulted to zero. Zero
    happens to be numerically harmless, but it is not the same thing: a
    transfer whose size could not be read is unknown, and treating it as a
    known nothing is how an unreadable row becomes a wrong balance later."""
    for flow in flows or []:
        try:
            amount = float(flow.get("amount"))
        except (AttributeError, TypeError, ValueError):
            continue
        yield flow.get("kind", ""), amount


def _group_exposure(positions: list, by: str) -> dict:
    """Open notional grouped by asset class or venue.

    A digest that omits this hides the one thing that can make a good P&L
    number dangerous: all of it riding on a single venue or a single class.
    """
    from alerts.formatting import class_label, venue_name

    out = {}
    for p in positions:
        key = class_label(p["symbol"]) if by == "class" else venue_name(p["exchange"])
        out[key] = round(out.get(key, 0.0) + _notional(p), 2)
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))


def _notional(pos: dict) -> float:
    """USD notional of an open position, using the risk manager's own
    conversion. `amount` means different things per venue — coins, base-currency
    units, or lots — so a plain amount x price reports 0.05 lots of gold as
    $100 when it is $10,000 of exposure, and makes the exposure percentages on
    this screen wrong in exactly the place where being wrong is expensive."""
    from core.risk_manager import position_notional_usd

    try:
        return float(position_notional_usd(pos))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _held(opened_at, closed_at) -> int:
    o, c = _parse_iso(opened_at), _parse_iso(closed_at)
    if not o or not c:
        return 0
    return max(0, int((c - o).total_seconds()))
