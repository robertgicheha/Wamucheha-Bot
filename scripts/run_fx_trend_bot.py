"""
Run the daily FX trend bot against MT5.

  python scripts/run_fx_trend_bot.py                 # dry-run loop (default, no orders)
  python scripts/run_fx_trend_bot.py --once          # one cycle, then exit
  python scripts/run_fx_trend_bot.py --send-orders   # real orders, DEMO accounts only
  python scripts/run_fx_trend_bot.py --send-orders --allow-live --i-understand-the-risk

Control (the admin dashboard does this for you):
  data/fx_trend.STOP  -> graceful exit, positions stay open
  data/fx_trend.KILL  -> flatten this bot's positions, then exit

Env: MT5_LOGIN, MT5_PASSWORD, MT5_SERVER (same as the main bot),
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional alerts).
Logs: data/logs/fx_trend.log (INFO+), data/logs/fx_trend_errors.log (WARNING+).
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.append(str(ROOT))

from core import auto_train
from core.fx_trend_broker import MT5Broker
from core.fx_trend_runner import FxTrendRunner, RunnerConfig
from strategy.fx_trend import TrendParams

log = logging.getLogger("fx_trend_bot")


def setup_logging():
    d = ROOT / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(),
              logging.handlers.RotatingFileHandler(d / "fx_trend.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
              logging.handlers.RotatingFileHandler(d / "fx_trend_errors.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")):
        h.setFormatter(fmt)
        if getattr(h, "baseFilename", "").endswith("fx_trend_errors.log"):
            h.setLevel(logging.WARNING)
        root.addHandler(h)


def make_notifier():
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")

    def notify(msg: str):
        if not (token and chat):
            return
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": f"[FX trend] {msg}"}, timeout=10)
        except Exception as e:
            from core.log_redact import redact
            log.warning("telegram failed: %s", redact(e))
    return notify


def interruptible_sleep(seconds: float, stop_file: Path, kill_file: Path) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if stop_file.exists() or kill_file.exists():
            return
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "fx_trend.yaml"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--send-orders", action="store_true", help="disable dry-run")
    ap.add_argument("--allow-live", action="store_true")
    ap.add_argument("--i-understand-the-risk", action="store_true")
    args = ap.parse_args()
    setup_logging()

    # .env (MT5 / Telegram creds) if python-dotenv is available
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    y = yaml.safe_load(open(args.config))["fx_trend"]
    sch, rk = y["schedule"], y["risk"]
    cfg = RunnerConfig(
        pairs=y["pairs"], params=TrendParams(**y["strategy"]), dry_run=not args.send_orders,
        max_open_positions=rk["max_open_positions"], max_net_per_currency=rk["max_net_per_currency"],
        daily_loss_halt_pct=rk["daily_loss_halt_pct"], weekly_loss_halt_pct=rk["weekly_loss_halt_pct"],
        max_spread_pips=rk["max_spread_pips"], max_lots_per_order=rk["max_lots_per_order"],
        max_consecutive_losses=rk.get("max_consecutive_losses", 0),
        loss_streak_reset=str(rk.get("loss_streak_reset") or ""),
        kill_file=sch["kill_file"], state_file=sch["state_file"],
        snapshot_file=sch.get("snapshot_file", "data/fx_trend_snapshot.json"),
        journal_file=sch.get("journal_file", "data/fx_trend.db"),
    )
    if y.get("dry_run") is False and not args.send_orders:
        log.info("config says dry_run=false but --send-orders not passed: staying in dry-run.")

    broker = MT5Broker(
        magic=y["magic"], suffix=y.get("symbol_suffix", ""),
        login=int(os.environ.get("MT5_LOGIN", "0") or 0),
        password=os.environ.get("MT5_PASSWORD", ""), server=os.environ.get("MT5_SERVER", ""))
    if not broker.connect():
        log.error("MT5 connection failed.")
        sys.exit("MT5 connection failed.")

    notify = make_notifier()
    if not cfg.dry_run and not broker.is_demo():
        if not (args.allow_live and args.i_understand_the_risk):
            log.error("REFUSING to send orders on a LIVE account.")
            sys.exit("REFUSING to send orders on a LIVE account. Validate on demo for 2-3 months first; "
                     "then pass --allow-live --i-understand-the-risk.")
        notify("WARNING: running with REAL MONEY.")

    stop_file, kill_file = ROOT / "data" / "fx_trend.STOP", ROOT / cfg.kill_file
    pid_file, hb = ROOT / "data" / "fx_trend.pid", ROOT / sch["heartbeat_file"]
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    stop_file.unlink(missing_ok=True)

    train_cfg = auto_train.settings(y.get("auto_train"))
    store = auto_train.Store(ROOT)
    live_account = not cfg.dry_run and not broker.is_demo()
    tuner = auto_train.Tuner(TrendParams(**y["strategy"]), store, train_cfg, live_account)
    trainer = auto_train.BackgroundTrainer(ROOT, store, train_cfg, str(Path(args.config).resolve()), notify)
    runner = FxTrendRunner(broker, cfg, notify=notify, base_dir=ROOT, tuner=tuner)
    msg = f"started ({'DRY-RUN' if cfg.dry_run else 'ORDERS ENABLED'}), pairs={cfg.pairs}"
    log.info(msg)
    notify(msg)
    runner._event("lifecycle", msg)
    try:
        while True:
            if stop_file.exists():
                log.info("STOP file found: exiting, positions left open.")
                runner._event("lifecycle", "graceful stop requested, positions left open")
                break
            try:
                result = runner.tick()
            except Exception as e:  # never die silently, never die from one bad cycle
                log.exception("cycle failed")
                runner._event("error", f"cycle failed: {e}", level="ERROR")
                notify(f"CYCLE ERROR: {e}")
                result = {"skipped": "error"}
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "pid": os.getpid(), "mode": "dry_run" if cfg.dry_run else "orders",
                                      "last_result": result.get("skipped") or "acted",
                                      "equity": result.get("equity")}))
            if result.get("skipped") == "kill_switch" or args.once:
                break
            trainer.poll()      # starts or collects a background retraining run when one is due
            interruptible_sleep(sch["poll_seconds"], stop_file, kill_file)
    finally:
        pid_file.unlink(missing_ok=True)
        notify("stopped")
        runner._event("lifecycle", "bot process exited")
        broker.shutdown() if hasattr(broker, "shutdown") else None


if __name__ == "__main__":
    main()
