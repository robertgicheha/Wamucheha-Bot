"""
Self-retraining for the daily trend bot, as champion against challenger.

"Retraining" here means re-choosing the trend rules and their lengths from a fixed list.
Picking whatever did best lately is how a bot fits itself to noise, so a challenger only
replaces the current settings (the champion) if it passes every gate below:

  1. Walk-forward: repeatedly choose on past years, then score the NEXT year, which the
     choice never saw. This tests the choosing process itself.
  2. Hold-out: the challenger is chosen without the most recent years, then must beat the
     champion on them.
  3. Robustness: most candidates must have made money, so the result does not hang on
     one lucky setting.
  4. Monte Carlo: its trades, reshuffled, must rarely end in a loss.
  5. A minimum gap between promotions.

Hard limits: only rule type, lengths and stop width are ever tuned. Risk per trade, loss
halts, lot caps and the long-only setting always come from config/fx_trend.yaml. If the
champion has been losing, risk can be scaled DOWN, never up.

Re-running every week looks at the same hold-out again and again, so passing is weaker
evidence than it appears. None of this predicts or guarantees profit.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.fx_trend import TrendParams, atr
from strategy.fx_trend_backtest import BtConfig, monte_carlo, pip_size, run_backtest, stats

logger = logging.getLogger("auto_train")

TUNABLE = ("rules", "breakout_len", "exit_len", "sma_len", "mom_len", "atr_stop_mult")
CHANNELS = ((20, 10), (55, 20), (100, 50))
SMA_LENGTHS = (50, 100, 200)
MOMENTUM_LENGTHS = (30, 90, 180)
STOP_WIDTHS = (2.5, 3.0, 4.0)

DEFAULTS = {
    "enabled": True,
    "auto_apply": True,             # false: report what it would change, change nothing
    "allow_apply_on_live": False,   # tuned settings are used in dry-run and on demo accounts only
    "interval_days": 7,
    "min_days_between_promotions": 28,
    "history_bars": 5000,
    "train_years": 5,
    "test_years": 1,
    "holdout_years": 2,
    "reduce_risk_when_degraded": True,
    "min_lot_units": {"XAUUSD": 1},  # smallest order in base units (0.01 lot of gold = 1 ounce)
    "degraded_risk_scale": 0.5,
    # Costs in PRICE units per instrument (gold: dollars per ounce). Use your broker's real ones.
    "costs": {"XAUUSD": {"spread": 0.30, "slippage": 0.10, "swap_bps_per_day": 1.4}},
    "gates": {
        "min_walkforward_sharpe": 0.30,
        "min_walkforward_profit_factor": 1.15,
        "min_walkforward_trades": 20,
        "max_drawdown_pct": 15.0,
        "min_sharpe_gain": 0.15,
        "max_extra_drawdown_pct": 2.0,
        "min_profitable_candidates_pct": 60.0,
        "max_probability_of_loss_pct": 35.0,
        "min_trades_to_select": 8,
    },
}


def settings(raw: dict | None) -> dict:
    """The auto_train block of fx_trend.yaml laid over the defaults."""
    raw = raw or {}
    out = {**DEFAULTS, **raw}
    out["gates"] = {**DEFAULTS["gates"], **(raw.get("gates") or {})}
    out["costs"] = {**DEFAULTS["costs"], **(raw.get("costs") or {})}
    out["degraded_risk_scale"] = min(1.0, max(0.0, float(out["degraded_risk_scale"])))   # only ever reduces
    return out


def tuned_values(p: TrendParams) -> dict:
    return {k: getattr(p, k) for k in TUNABLE}


def candidates(base: TrendParams) -> list[TrendParams]:
    """The whole search space. Everything not listed in TUNABLE is copied from `base`."""
    out = []
    for width in STOP_WIDTHS:
        for sma in SMA_LENGTHS:
            out += [replace(base, rules="breakout", breakout_len=entry, exit_len=leave, sma_len=sma,
                            atr_stop_mult=width) for entry, leave in CHANNELS]
            out.append(replace(base, rules="ensemble", sma_len=sma, atr_stop_mult=width))
            out += [replace(base, rules="momentum", mom_len=mom, sma_len=sma, atr_stop_mult=width)
                    for mom in MOMENTUM_LENGTHS]
    return out


def cost_config(pairs: list[str], cfg: dict) -> BtConfig:
    """Trading costs given in price units, converted to the backtester's pips."""
    spreads, swap = {}, 0.3
    for pair in pairs:
        cost = cfg["costs"].get(pair)
        if not cost:
            raise ValueError(f"no trading costs set for {pair}: add it under auto_train.costs")
        spreads[pair] = (float(cost["spread"]) + 2 * float(cost["slippage"])) / pip_size(pair)
        swap = max(swap, float(cost.get("swap_bps_per_day", 0.3)))
    return BtConfig(spread_pips=spreads, slippage_pips=0.0, swap_bps_per_day=swap)


def minimum_balances(frames: dict, params: TrendParams, cfg: dict) -> list:
    """The balance at which the broker's smallest order risks no more than risk_pct.
    Below it the live bot skips entries, so a profitable test earns nothing."""
    out = []
    for pair, df in frames.items():
        units = cfg["min_lot_units"].get(pair)
        if not units or not pair.endswith("USD") or params.risk_pct <= 0:
            continue
        risk = float(atr(df, params.atr_len).iloc[-1]) * params.atr_stop_mult * units
        out.append({"pair": pair, "smallest_trade_risk_usd": round(risk, 2),
                    "balance_needed_usd": round(risk / (params.risk_pct / 100))})
    return out


# ---------- evaluation ----------
def _window(result: dict, start, end) -> dict:
    """Statistics for [start, end) cut out of one continuous backtest."""
    curve = result["equity"]
    curve = curve[(curve.index >= start) & (curve.index < end)]
    trades = [t for t in result["trades"] if start <= t.entry_time < end]
    if len(curve) < 2:
        return {"n_trades": len(trades), "sharpe": 0.0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0,
                "profit_factor": None}
    return stats(curve, trades)


def _best(results: list, start, end, min_trades: int) -> int | None:
    """Index of the highest risk-adjusted candidate in a window, ignoring thin samples."""
    scored = []
    for i, result in enumerate(results):
        s = _window(result, start, end)
        if s["n_trades"] >= min_trades:
            scored.append((s["sharpe"], s["total_return_pct"], -i))
    return -max(scored)[2] if scored else None


def walk_forward(results: list, first, last, cfg: dict) -> dict:
    """Stitches together the unseen years that followed each choice."""
    years = lambda n: pd.DateOffset(years=n)
    start = first + years(cfg["train_years"])
    returns, trades, picks = [], [], []
    while start < last:
        end = start + years(cfg["test_years"])
        pick = _best(results, start - years(cfg["train_years"]), start, cfg["gates"]["min_trades_to_select"])
        if pick is not None:
            curve = results[pick]["equity"]
            step = curve.pct_change()
            returns.append(step[(step.index >= start) & (step.index < end)])
            trades += [t for t in results[pick]["trades"] if start <= t.entry_time < end]
            picks.append({"test_from": str(start.date()), "candidate": pick})
        start = end
    if not returns:
        return {"stats": {"n_trades": 0}, "picks": picks}
    curve = (1 + pd.concat(returns).fillna(0)).cumprod()
    return {"stats": stats(curve, trades), "picks": picks}


def evaluate(frames: dict, champion: TrendParams, cfg: dict, base: TrendParams | None = None) -> dict:
    """Runs every test and returns the report, including the verdict. Changes nothing."""
    base = base or champion
    gates = cfg["gates"]
    grid = candidates(base)
    bt = cost_config(list(frames), cfg)
    first = max(df.index[0] for df in frames.values())
    last = min(df.index[-1] for df in frames.values())
    needed = cfg["train_years"] + cfg["holdout_years"] + 1
    span = (last - first).days / 365.25
    if span < needed:
        raise ValueError(f"need at least {needed} years of daily history, have {span:.1f}")

    results = [run_backtest(frames, p, bt) for p in grid]
    champion_result = run_backtest(frames, champion, bt)
    end = last + pd.Timedelta(days=1)
    holdout_start = last - pd.DateOffset(years=cfg["holdout_years"])

    wf = walk_forward(results, first, holdout_start, cfg)
    recent_wf = walk_forward(results, first, end, cfg)
    for fold in recent_wf["picks"]:
        fold["params"] = tuned_values(grid[fold.pop("candidate")])

    # Chosen using nothing from the hold-out period.
    pick = _best(results, holdout_start - pd.DateOffset(years=cfg["train_years"]), holdout_start,
                 gates["min_trades_to_select"])
    if pick is None:
        raise ValueError("no candidate traded often enough to be judged")
    challenger, challenger_result = grid[pick], results[pick]
    ch = _window(challenger_result, holdout_start, end)
    now = _window(champion_result, holdout_start, end)
    profitable = sum(_window(r, first, holdout_start)["total_return_pct"] > 0 for r in results) / len(results) * 100
    mc = monte_carlo(challenger_result["trades"])
    w = recent_wf["stats"]

    def gate(name, passed, detail):
        return {"gate": name, "passed": bool(passed), "detail": detail}

    checks = [
        gate("walk-forward risk-adjusted return",
             w.get("sharpe", 0) >= gates["min_walkforward_sharpe"] and w.get("total_return_pct", 0) > 0,
             f"Sharpe {w.get('sharpe')}, return {w.get('total_return_pct')}% on unseen years"),
        gate("walk-forward profit factor",
             (w.get("profit_factor") or 0) >= gates["min_walkforward_profit_factor"],
             f"{w.get('profit_factor')} (needs {gates['min_walkforward_profit_factor']})"),
        gate("enough walk-forward trades", w.get("n_trades", 0) >= gates["min_walkforward_trades"],
             f"{w.get('n_trades', 0)} trades (needs {gates['min_walkforward_trades']})"),
        gate("walk-forward drawdown", w.get("max_drawdown_pct", -100) >= -gates["max_drawdown_pct"],
             f"{w.get('max_drawdown_pct')}% (limit -{gates['max_drawdown_pct']}%)"),
        gate("made money on the hold-out", ch["total_return_pct"] > 0 and ch["n_trades"] > 0,
             f"{ch['total_return_pct']}% from {ch['n_trades']} trades"),
        gate("clearly better than the current settings", ch["sharpe"] >= now["sharpe"] + gates["min_sharpe_gain"],
             f"hold-out Sharpe {ch['sharpe']} against {now['sharpe']}"),
        gate("drawdown no worse",
             ch["max_drawdown_pct"] >= now["max_drawdown_pct"] - gates["max_extra_drawdown_pct"],
             f"{ch['max_drawdown_pct']}% against {now['max_drawdown_pct']}%"),
        gate("not one lucky setting", profitable >= gates["min_profitable_candidates_pct"],
             f"{profitable:.0f}% of {len(grid)} candidates made money before the hold-out"),
        gate("reshuffled trades rarely lose",
             mc.get("prob_loss_pct", 100) <= gates["max_probability_of_loss_pct"],
             f"chance of loss {mc.get('prob_loss_pct', 'unknown')}%"),
    ]
    same = tuned_values(challenger) == tuned_values(champion)
    degraded = now["total_return_pct"] < 0 or now["max_drawdown_pct"] < -gates["max_drawdown_pct"]
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs": sorted(frames), "data_from": str(first.date()), "data_to": str(last.date()),
        "holdout_from": str(holdout_start.date()), "candidates": len(grid),
        "champion": {"params": tuned_values(champion), "holdout": now,
                     "full_period": _window(champion_result, first, end)},
        "challenger": {"params": tuned_values(challenger), "holdout": ch,
                       "full_period": _window(challenger_result, first, end), "monte_carlo": mc},
        "walk_forward": w, "walk_forward_before_holdout": wf["stats"], "walk_forward_picks": recent_wf["picks"],
        "checks": checks, "same_as_champion": same,
        "passed": not same and all(c["passed"] for c in checks),
        "champion_degraded": degraded,
        "minimum_balance": minimum_balances(frames, champion, cfg),
    }


# ---------- state on disk ----------
class Store:
    def __init__(self, base_dir: Path | str):
        self.dir = Path(base_dir) / "data" / "auto_train"
        self.champion_file = self.dir / "champion.json"
        self.report_file = self.dir / "last_report.json"
        self.status_file = self.dir / "status.json"
        self.history_file = self.dir / "history.jsonl"

    @staticmethod
    def read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, path: Path, value: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, default=float), encoding="utf-8")
        os.replace(tmp, path)

    def log(self, event: str, **fields) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with open(self.history_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, default=float) + "\n")


def champion_params(base: TrendParams, saved: dict) -> TrendParams | None:
    """The promoted settings laid over `base`, or None if the file must not be trusted:
    nothing promoted, the yaml strategy changed since (your edit wins), or values that
    are not in the search space."""
    values = saved.get("params")
    if not isinstance(values, dict) or set(values) != set(TUNABLE):
        return None
    if saved.get("config_params") != tuned_values(base):
        return None
    for candidate in candidates(base):
        if tuned_values(candidate) == values:
            return candidate
    return None


def active_settings(base: TrendParams, store: Store, cfg: dict, live_account: bool) -> tuple:
    """(params to trade with, risk scale from 0 to 1, short explanation)."""
    if not cfg["enabled"]:
        return base, 1.0, "auto-training is off"
    status = store.read(store.status_file)
    scale = 1.0
    if cfg["reduce_risk_when_degraded"] and status.get("degraded") and status.get("config_params") == tuned_values(base):
        scale = cfg["degraded_risk_scale"]
    if live_account and not cfg["allow_apply_on_live"]:
        return base, scale, "live account: config settings only"
    promoted = champion_params(base, store.read(store.champion_file)) if cfg["auto_apply"] else None
    return (promoted, scale, "promoted settings") if promoted else (base, scale, "config settings")


def _days_since(stamp: str | None, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 86400
    except (TypeError, ValueError):
        return float("inf")


def is_due(store: Store, cfg: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    status = store.read(store.status_file)
    # After a failed attempt wait a day, so a missing data source is not retried every minute.
    if _days_since(status.get("last_attempt"), now) < 1:
        return False
    return cfg["enabled"] and _days_since(status.get("last_success"), now) >= cfg["interval_days"]


def mark_attempt(store: Store) -> None:
    status = store.read(store.status_file)
    status["last_attempt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.write(store.status_file, status)


def train(frames: dict, base: TrendParams, store: Store, cfg: dict, source: str,
          now: datetime | None = None) -> dict:
    """One retraining run: evaluate, then promote only if every gate passed."""
    now = now or datetime.now(timezone.utc)
    saved = store.read(store.champion_file)
    champion = champion_params(base, saved) or base
    report = evaluate(frames, champion, cfg, base=base)
    report["data_source"] = source

    since = _days_since(saved.get("promoted_at"), now)
    if not report["passed"]:
        decision = "kept the current settings"
    elif since < cfg["min_days_between_promotions"]:
        decision = f"passed, but the last change was only {since:.0f} day(s) ago: kept the current settings"
    elif not cfg["auto_apply"]:
        decision = "passed: recommended, not applied (auto_apply is off)"
    else:
        previous = {k: v for k, v in saved.items() if k != "previous"} or None
        store.write(store.champion_file, {
            "params": report["challenger"]["params"], "config_params": tuned_values(base),
            "promoted_at": now.isoformat(timespec="seconds"), "previous": previous})
        decision = "PROMOTED the challenger (used in dry-run and demo once no position is open)"
        # Health now refers to the settings that will trade from here on.
        held = report["challenger"]["holdout"]
        report["champion_degraded"] = held["total_return_pct"] < 0
    report["decision"] = decision

    store.write(store.report_file, report)
    status = store.read(store.status_file)
    status.update(last_success=now.isoformat(timespec="seconds"), degraded=report["champion_degraded"],
                  config_params=tuned_values(base), decision=decision)
    store.write(store.status_file, status)
    store.log("train", decision=decision, degraded=report["champion_degraded"],
              challenger=report["challenger"]["params"],
              failed_gates=[c["gate"] for c in report["checks"] if not c["passed"]])
    return report


def rollback(store: Store) -> str:
    """Back to the settings before the last promotion (or to the yaml if there were none)."""
    saved = store.read(store.champion_file)
    if not saved:
        return "nothing to roll back: the config settings are already in use"
    if saved.get("previous"):
        store.write(store.champion_file, {**saved["previous"], "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        message = "restored the previously promoted settings"
    else:
        store.champion_file.unlink()
        message = "removed the promoted settings: config/fx_trend.yaml is in use again"
    store.log("rollback", result=message)
    return message


class Tuner:
    """What the runner asks before each decision: which settings, and how much risk."""

    def __init__(self, base: TrendParams, store: Store, cfg: dict, live_account: bool):
        self.base, self.store, self.cfg, self.live_account = base, store, cfg, live_account

    def __call__(self) -> tuple:
        return active_settings(self.base, self.store, self.cfg, self.live_account)

    def restore(self, values: dict) -> TrendParams | None:
        """Settings an open position was started under, if they are still legitimate."""
        return champion_params(self.base, {"params": values, "config_params": tuned_values(self.base)})


class BackgroundTrainer:
    """Starts scripts/run_auto_train.py when due, in its own process so that a slow or
    failing training run can never stall or crash the trading loop."""

    def __init__(self, root: Path, store: Store, cfg: dict, config_path: str, notify, spawn=None):
        self.root, self.store, self.cfg, self.notify = Path(root), store, cfg, notify
        self.config_path = config_path
        self.spawn = spawn or self._spawn
        self.process = None

    def _spawn(self):
        log_file = self.root / "data" / "logs" / "auto_train.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as out:
            return subprocess.Popen([sys.executable, "-m", "scripts.run_auto_train", "--config", self.config_path], cwd=self.root, stdout=out,
                                    stderr=subprocess.STDOUT)

    def poll(self) -> None:
        try:
            if self.process is not None:
                code = self.process.poll()
                if code is None:
                    return
                self.process = None
                if code == 0:
                    report = self.store.read(self.store.report_file)
                    if report:
                        self.notify(summary(report))
                else:
                    self.notify("Auto-training failed (see data/logs/auto_train.log). Settings are unchanged; "
                                "it will retry tomorrow.")
            elif is_due(self.store, self.cfg):
                self.process = self.spawn()
                mark_attempt(self.store)     # a child that dies at once must not be restarted every poll
                logger.info("auto-training started")
        except Exception:
            logger.exception("auto-training could not be managed")


def summary(report: dict) -> str:
    """A few lines for alerts and the console."""
    failed = [c["gate"] for c in report["checks"] if not c["passed"]]
    lines = [f"Auto-training ({', '.join(report['pairs'])}, data to {report['data_to']}): {report['decision']}."]
    if failed and not report["same_as_champion"]:
        lines.append("Failed: " + "; ".join(failed) + ".")
    if report["same_as_champion"]:
        lines.append("The best candidate is the current setting.")
    if report["champion_degraded"]:
        lines.append("WARNING: the settings in use have been losing on recent data.")
    for need in report.get("minimum_balance", []):
        lines.append(f"{need['pair']}: the smallest order risks ${need['smallest_trade_risk_usd']:,.0f}, so entries "
                     f"are skipped below a balance of about ${need['balance_needed_usd']:,}.")
    return " ".join(lines)
