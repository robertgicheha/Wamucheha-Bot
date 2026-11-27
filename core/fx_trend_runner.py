"""
Live runner for the daily FX trend-following bot.

tick()      -> called every poll interval (cheap): refreshes the dashboard
               snapshot, syncs closed trades, then run_once().
run_once()  -> one decision cycle, acting at most once per new closed daily bar.
               Idempotent: restarts and polling can never double-trade.

Pipeline (mirrors the backtest, strategy/fx_trend_backtest.py):
  closed daily bars -> compute_indicators (pure) -> exits / stop trailing
  -> risk gates -> sizing from the stop -> orders (with SL attached)

Safety rails (all on by default):
  * dry_run=True: logs what it WOULD do, sends nothing
  * every order carries a stop-loss at send time
  * only touches positions with its own magic number
  * kill switch file: next cycle flattens and stops
  * daily / weekly loss halts, max positions, per-currency exposure cap,
    spread guard, lots rounded DOWN so risk is never exceeded
  * broker positions are the source of truth each cycle (reconciliation)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from core.auto_train import TUNABLE
from core.fx_trend_analytics import flip_analysis, signal_levels, strength
from core.fx_trend_broker import Broker, Position
from core.fx_trend_journal import Journal
from strategy.fx_trend import (
    TrendParams, compute_indicators, currency_legs, initial_stop, trail_stop,
)

logger = logging.getLogger("fx_trend_runner")

TRADE_SYNC_SECONDS = 300


@dataclass
class RunnerConfig:
    pairs: list[str] = field(default_factory=lambda: [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY", "GBPJPY"])
    params: TrendParams = field(default_factory=TrendParams)
    dry_run: bool = True
    history_bars: int = 400
    max_open_positions: int = 6
    max_net_per_currency: int = 3
    daily_loss_halt_pct: float = 2.0
    weekly_loss_halt_pct: float = 4.0
    max_spread_pips: float = 3.0
    max_lots_per_order: float = 1.0
    max_consecutive_losses: int = 0        # 0 = off
    loss_streak_reset: str = ""            # ignore trades closed before this ISO time
    kill_file: str = "data/fx_trend.KILL"
    state_file: str = "data/fx_trend_state.json"
    snapshot_file: str = "data/fx_trend_snapshot.json"
    journal_file: str = "data/fx_trend.db"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)


class FxTrendRunner:
    def __init__(self, broker: Broker, cfg: RunnerConfig,
                 notify: Callable[[str], None] = lambda msg: None,
                 base_dir: Path | str = ".", journal: Journal | None = None, tuner=None):
        self.tuner = tuner                  # core.auto_train.Tuner, or None for config settings only
        self._risk_scale = 1.0
        self.broker, self.cfg, self.notify = broker, cfg, notify
        self.base = Path(base_dir)
        self.journal = journal if journal is not None else Journal(self.base / cfg.journal_file)
        self._last_trade_sync: datetime | None = None
        self._halted = False

    # ------------------------------------------------------------------
    def _event(self, kind: str, msg: str, pair: str = "", level: str = "INFO") -> None:
        try:
            self.journal.add_event(kind, msg, pair, level)
        except Exception:
            logger.exception("journal write failed")

    def _act(self, kind: str, pair: str, msg: str) -> None:
        tag = "[DRY] " if self.cfg.dry_run else ""
        logger.info(tag + msg)
        self.notify(tag + msg)
        self._event(kind, tag + msg, pair)

    def _flatten(self, reason: str) -> None:
        for pos in self.broker.positions():
            self._act("close", pos.pair, f"CLOSE {pos.pair} ({reason})")
            if not self.cfg.dry_run:
                self.broker.close(pos)

    def _apply_tuning(self) -> None:
        """Takes up retrained settings, but never swaps the rules under an open position."""
        if self.tuner is None:
            return
        state_path = self.base / self.cfg.state_file
        try:
            wanted, scale, note = self.tuner()
            state = _load(state_path)
            if self.broker.positions():
                # Keep (or, after a restart, restore) the settings the position was opened under.
                wanted = self.tuner.restore(state.get("active_params")) or self.cfg.params
            scale = min(1.0, max(0.0, float(scale)))          # tuning can only ever reduce risk
            if wanted != self.cfg.params:
                self.cfg.params = wanted
                msg = (f"strategy settings now: {wanted.rules} (breakout {wanted.breakout_len}/{wanted.exit_len}, "
                       f"SMA {wanted.sma_len}, momentum {wanted.mom_len}, stop {wanted.atr_stop_mult} x ATR) [{note}]")
                self.notify("FX trend bot: " + msg)
                self._event("tuning", msg)
            if scale != self._risk_scale:
                self._risk_scale = scale
                msg = (f"risk per trade reduced to {scale:.0%} of normal: recent results are negative"
                       if scale < 1 else "risk per trade is back to normal")
                self.notify("FX trend bot: " + msg)
                self._event("tuning", msg, level="WARNING" if scale < 1 else "INFO")
            values = {k: getattr(self.cfg.params, k) for k in TUNABLE}
            if state.get("active_params") != values:
                state["active_params"] = values
                _save(state_path, state)
        except Exception:
            logger.exception("tuning failed: keeping the current settings")

    # ------------------------------------------------------------------
    def snapshot(self, now: datetime | None = None) -> dict:
        """Cheap live view for the dashboard (account + positions)."""
        now = now or datetime.now(timezone.utc)
        state = _load(self.base / self.cfg.state_file)
        positions = []
        for p in self.broker.positions():
            q = self.broker.quote(p.pair)
            px = (q.bid if p.side > 0 else q.ask) if q else None
            positions.append({
                "ticket": p.ticket, "pair": p.pair, "side": p.side, "volume": p.volume,
                "entry": p.entry, "sl": p.sl, "price": px, "profit": p.profit,
                "open_time": p.open_time,
                "dist_to_sl_pct": round(abs(px - p.sl) / px * 100, 3) if px and p.sl else None,
            })
        snap = {
            "ts": now.isoformat(timespec="seconds"),
            "mode": "dry_run" if self.cfg.dry_run else "orders",
            "account": self.broker.account_summary(),
            "positions": positions,
            "floating_pnl": round(sum(p["profit"] for p in positions), 2),
            "last_bar": state.get("last_bar"),
            "halted": self._halted,
            "pairs": self.cfg.pairs,
        }
        _save(self.base / self.cfg.snapshot_file, snap)
        return snap

    def sync_trades(self, now: datetime, force: bool = False) -> int:
        if not force and self._last_trade_sync and (now - self._last_trade_sync).total_seconds() < TRADE_SYNC_SECONDS:
            return 0
        self._last_trade_sync = now
        try:
            trades = self.broker.closed_trades(now - timedelta(days=400))
            self.journal.upsert_trades(trades)
            return len(trades)
        except Exception:
            logger.exception("trade sync failed")
            return 0

    def losing_streak(self) -> int:
        """Losses in a row, newest first, net of commission and swap."""
        streak = 0
        for t in self.journal.trades(200):
            if (t["close_time"] or "") <= self.cfg.loss_streak_reset:
                break
            if t["profit"] + (t["commission"] or 0) + (t["swap"] or 0) > 0:
                break
            streak += 1
        return streak

    def tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        try:
            self.snapshot(now)
            self.sync_trades(now)
        except Exception:
            logger.exception("snapshot/sync failed")
        return self.run_once(now)

    # ------------------------------------------------------------------
    def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        if not (self.base / self.cfg.kill_file).exists():
            self._apply_tuning()
        cfg, p = self.cfg, self.cfg.params
        state_path = self.base / cfg.state_file
        state = _load(state_path)
        summary = {"actions": [], "skipped": None}

        # ---- kill switch -------------------------------------------------
        if (self.base / cfg.kill_file).exists():
            self._flatten("KILL SWITCH")
            self._event("kill", "Kill switch active: flattened and stopped.", level="WARNING")
            self.notify("FX trend bot: kill switch active, flattened and stopped.")
            summary["skipped"] = "kill_switch"
            return summary

        # ---- cheap new-bar probe (avoids downloading history every poll) --
        probe = self.broker.latest_bar_date(cfg.pairs[0]) if cfg.pairs else None
        if probe and probe == state.get("last_bar"):
            summary["skipped"] = "already_processed"
            return summary

        # ---- data --------------------------------------------------------
        frames = {}
        for pair in cfg.pairs:
            df = self.broker.daily_bars(pair, cfg.history_bars)
            if len(df) >= p.warmup + 5:
                frames[pair] = df
            else:
                logger.warning("%s: only %d bars, skipped", pair, len(df))
                self._event("data", f"{pair}: only {len(df)} bars, skipped", pair, "WARNING")
        if not frames:
            summary["skipped"] = "no_data"
            self._event("data", "NO DATA from broker, nothing done", level="ERROR")
            self.notify("FX trend bot: NO DATA from broker, nothing done.")
            return summary

        latest_bar = max(df.index[-1] for df in frames.values()).strftime("%Y-%m-%d")
        if state.get("last_bar") == latest_bar:
            summary["skipped"] = "already_processed"
            return summary

        # ---- loss halts --------------------------------------------------
        equity = self.broker.equity()
        iso = now.isocalendar()
        wk = f"{iso.year}-W{iso.week:02d}"
        if state.get("week") != wk:
            state["week"], state["week_start_equity"] = wk, equity
        prev_eq = state.get("prev_equity", equity)
        daily_pct = (equity / prev_eq - 1) * 100 if prev_eq else 0.0
        weekly_pct = (equity / state["week_start_equity"] - 1) * 100 if state.get("week_start_equity") else 0.0
        halted = daily_pct <= -cfg.daily_loss_halt_pct or weekly_pct <= -cfg.weekly_loss_halt_pct
        self.sync_trades(now, force=True)      # count stops hit since the last cycle
        streak = self.losing_streak()
        streak_halt = bool(cfg.max_consecutive_losses) and streak >= cfg.max_consecutive_losses
        if streak_halt:
            msg = (f"entries HALTED after {streak} losses in a row. Review, then set "
                   f"risk.loss_streak_reset in the config and restart to resume.")
            self.notify("FX trend bot: " + msg)
            self._event("halt", msg, level="WARNING")
        halted = halted or streak_halt
        self._halted = halted
        if halted and not streak_halt:
            msg = f"entries HALTED (day {daily_pct:.2f}%, week {weekly_pct:.2f}%)"
            self.notify("FX trend bot: " + msg)
            self._event("halt", msg, level="WARNING")

        rows = {pair: compute_indicators(df, p).iloc[-1] for pair, df in frames.items()}

        # ---- journal the analysis for the dashboard ------------------------
        sig_rows = []
        for pair, row in rows.items():
            if p.rules in ("breakout", "ensemble"):  # the closes that would flip the signal next bar
                up = max(float(frames[pair]["high"].tail(p.breakout_len).max()), float(row["sma"]))
                dn = max(float(frames[pair]["low"].tail(p.exit_len).min()), float(row["sma"]))
            else:
                up, dn = signal_levels(frames[pair]["close"], float(row["sma"]), p.mom_len)
            sig_rows.append({
                "pair": pair, "side": int(row["signal"]), "close": float(row["close"]),
                "sma": float(row["sma"]), "mom": float(row["mom"]), "atr": float(row["atr"]),
                "strength": strength(float(row["close"]), float(row["sma"]), float(row["atr"])),
                "up_level": up, "dn_level": dn,
                **flip_analysis(int(row["signal"]), float(row["close"]), float(row["atr"]), up, dn),
            })
        self.journal.add_signals(latest_bar, sig_rows)

        # ---- 1. manage existing positions ---------------------------------
        positions = [pos for pos in self.broker.positions() if pos.pair in frames]
        remaining: list[Position] = []
        for pos in positions:
            row = rows[pos.pair]
            want = int(row["signal"])
            if want != pos.side:
                self._act("close", pos.pair, f"CLOSE {pos.pair} signal {pos.side:+d} -> {want:+d}")
                summary["actions"].append(("close", pos.pair))
                if not cfg.dry_run and not self.broker.close(pos):
                    self._event("order", f"CLOSE FAILED {pos.pair}", pos.pair, "ERROR")
                    remaining.append(pos)  # still exposed: keep counting it
                continue
            base_sl = pos.sl or initial_stop(pos.side, pos.entry, row["atr"], p.atr_stop_mult)
            new_sl = trail_stop(pos.side, base_sl, row["high"], row["low"], row["atr"], p.atr_stop_mult)
            improved = (pos.sl == 0) or (new_sl > pos.sl if pos.side > 0 else new_sl < pos.sl)
            if improved:
                self._act("trail", pos.pair, f"TRAIL {pos.pair} SL {pos.sl:.5f} -> {new_sl:.5f}")
                summary["actions"].append(("trail", pos.pair))
                if not cfg.dry_run:
                    if self.broker.modify_sl(pos, new_sl):
                        pos.sl = new_sl
                    else:
                        self._event("order", f"SL MODIFY FAILED {pos.pair}", pos.pair, "ERROR")
            remaining.append(pos)

        # ---- 2. entries ----------------------------------------------------
        open_pairs = {pos.pair for pos in remaining}
        net: dict[str, int] = {}
        for pos in remaining:
            for c, s in currency_legs(pos.pair, pos.side).items():
                net[c] = net.get(c, 0) + s

        def skip(pair, why):
            logger.info("%s skipped: %s", pair, why)
            self._event("skip", f"{pair} entry skipped: {why}", pair)

        if not halted:
            for pair, row in rows.items():
                want = int(row["signal"])
                if want == 0 or pair in open_pairs:
                    continue
                if len(open_pairs) >= cfg.max_open_positions:
                    skip(pair, "max open positions reached")
                    continue
                legs = currency_legs(pair, want)
                if any(abs(net.get(c, 0) + s) > cfg.max_net_per_currency for c, s in legs.items()):
                    skip(pair, "currency exposure cap")
                    continue
                q = self.broker.quote(pair)
                if q is None:
                    skip(pair, "no quote")
                    continue
                if q.spread_pips > cfg.max_spread_pips:
                    skip(pair, f"spread {q.spread_pips:.1f} pips > {cfg.max_spread_pips}")
                    continue
                entry = q.ask if want > 0 else q.bid
                stop = initial_stop(want, entry, row["atr"], p.atr_stop_mult)
                risk_pct = p.risk_pct * self._risk_scale
                lots = min(self.broker.lots_for_risk(pair, want, equity * risk_pct / 100.0, entry, stop),
                           cfg.max_lots_per_order)
                if lots <= 0:
                    skip(pair, "risk-sized lot below broker minimum")
                    continue
                self._act("open", pair, f"OPEN {pair} {'LONG' if want > 0 else 'SHORT'} {lots} lots "
                                        f"@~{entry:.5f} SL {stop:.5f} (risk {risk_pct:g}%)")
                if not cfg.dry_run and self.broker.open(pair, want, lots, stop) is None:
                    self.notify(f"FX trend bot: ORDER FAILED {pair}")
                    self._event("order", f"OPEN FAILED {pair}", pair, "ERROR")
                    continue
                summary["actions"].append(("open", pair))
                open_pairs.add(pair)
                for c, s in legs.items():
                    net[c] = net.get(c, 0) + s

        # ---- persist ------------------------------------------------------
        self.journal.add_cycle(latest_bar, equity, daily_pct, weekly_pct, halted, len(open_pairs),
                               "halted" if halted else "ok")
        state.update(last_bar=latest_bar, prev_equity=equity,
                     last_run=now.isoformat(timespec="seconds"))
        _save(state_path, state)
        self.sync_trades(now, force=True)
        summary["equity"] = equity
        return summary
