# Trading Bot — Stake-Protected Compounding System

A risk-first algorithmic trading system for crypto/forex/gold (short-term automated
execution) plus a non-predictive equity screener/alert system for stocks/ETFs
(long-term, human-in-the-loop).

## Honesty first
No bot can guarantee profit or "tell you which stock makes you richest." This system
is built to (a) protect your capital aggressively, (b) fail loudly and safely rather
than silently, and (c) give you evidence-based alerts for long-term investing — not
predictions dressed up as certainty. Backtest and paper-trade for months before real
money touches it.

## Architecture (see docs/ARCHITECTURE.md)
- **VPS (primary)**: runs the bot 24/7 — data ingestion, strategy, execution, risk manager.
- **Local machine (watchdog + backup)**: independently monitors the VPS heartbeat,
  can trigger an emergency flatten/kill-switch if the VPS goes dark, and pulls nightly
  backups of trade state.
- **Dashboard**: FastAPI app, viewable from either machine.
- **Alerts**: Telegram + Discord + Email, all fired from the same event bus so you
  never depend on a single channel.

## What's now implemented
- `strategy/technical_strategy.py` — EMA(9/21) crossover + RSI filter short-term signal
- `strategy/backtester.py` + `scripts/run_backtest.py` — backtest with fees, slippage,
  and a walk-forward train/test split so you can catch overfitting before going live:
  `python scripts/run_backtest.py --exchange binance --symbol BTC/USDT --timeframe 15m`
- `long_term/screener.py` + `long_term/fundamentals.py` — dividend/fundamentals
  screener with disclosed pass/fail reasons (not a black-box prediction)
- `long_term/scheduler.py` — runs the screener weekly and alerts through the same
  Telegram/Discord/email pipeline as trading alerts
- `ml/lstm_predictor.py` + `ml/train_lstm.py` — PyTorch LSTM that predicts
  probability of upward price movement N bars ahead, wired in as an OPTIONAL
  extra filter (`strategy.technical_strategy.generate_signal_with_ml`) on top of
  the existing rule-based signal — it can only make the system more conservative
  (fewer trades), never trigger a trade the rule-based logic didn't already flag.
  Train it yourself on your target symbol first:
  `python ml/train_lstm.py --exchange binance --symbol BTC/USDT --timeframe 15m`
  and check the printed test accuracy against the naive baseline before trusting
  its output — an LSTM trained purely on price history frequently fails to beat
  simply predicting the majority class, which is a real result worth taking
  seriously, not a bug to work around.

## Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
```

Edit `config/config.yaml` for risk parameters, then:
```bash
python main.py          # on the VPS
python scripts/heartbeat_monitor.py   # on your local machine
```

## Read these before going live
- `docs/COMMON_MISTAKES.md` — real failure modes in retail trading bots, and how this
  system avoids each one.
- `docs/ARCHITECTURE.md` — full system design, backup strategy, failover.
- `docs/BACKUP_STRATEGY.md` — what's backed up, where, how often, how to restore.

## Not financial advice
This is engineering scaffolding, not investment advice. You are responsible for
regulatory compliance in your jurisdiction (in Kenya: Capital Markets Authority
rules on algorithmic/forex trading apply).
