# FX Trend Bot

Daily time-series-momentum bot for FX majors/crosses on MT5, with an admin dashboard.
No strategy is guaranteed to profit. Validate on a demo account first.

## Files
| File | Role |
|---|---|
| `strategy/fx_trend.py` | Pure strategy functions (signals, stops, sizing) |
| `strategy/fx_trend_backtest.py` | Portfolio backtest with costs, risk gates, Monte Carlo |
| `core/fx_trend_runner.py` | Live decision cycle (idempotent, once per closed daily bar) |
| `core/fx_trend_broker.py` | MT5 adapter (own magic number only) |
| `core/fx_trend_journal.py` | SQLite journal: cycles, signals, events, trades |
| `core/fx_trend_analytics.py` | Performance + predictive analytics |
| `core/fx_trend_supervisor.py` | Start / stop / restart / flatten the bot process |
| `dashboard/fx_admin.py` + `templates/fx_admin.html` | Admin dashboard |
| `config/fx_trend.yaml` | All settings |

## Workflow
1. `python scripts/run_fx_trend_backtest.py --csv-dir <dir> --holdout-start 2022-01-01` (or `--mt5`).
   Writes `data/fx_trend_backtest.json` (shown on the dashboard).
2. Set `FX_ADMIN_TOKEN` in `.env`, then `python -m dashboard.fx_admin` -> http://127.0.0.1:8090
   (binds to localhost; use an SSH tunnel or a TLS reverse proxy for remote access).
3. Start the bot from the dashboard's Control tab (dry-run), or:
   `python scripts/run_fx_trend_bot.py` (dry-run) / `--send-orders` (demo accounts only).
4. After 2-3 months of demo results that match the backtest, consider live:
   `--send-orders --allow-live --i-understand-the-risk` (CLI only; the dashboard cannot do this).

## Control files (in `data/`)
`fx_trend.STOP` graceful exit, positions stay open. `fx_trend.KILL` flatten this bot's positions, then exit
(stays until cleared). Logs: `data/logs/fx_trend.log`, `fx_trend_errors.log`, `fx_trend_console.log`.

## Tests
`pytest tests/test_fx_trend.py tests/test_fx_trend_runner.py tests/test_fx_admin.py`

## Self-retraining
`core/auto_train.py`, run weekly by the bot in a separate process (settings: `auto_train` in `config/fx_trend.yaml`).

* Tries 63 settings: breakout, multi-length breakout ensemble and momentum rules, with three trend filters and three stop widths.
* The current settings are replaced only if a challenger passes all nine tests (walk-forward on unseen years,
  a hold-out it was not chosen on, drawdown, robustness, reshuffled trades) and 28 days have passed since the last change.
* Risk per trade, loss halts, lot caps and the long-only setting are never tuned. Losing recent results halve the risk.
* Promoted settings apply in dry-run and on demo accounts only, and only once no position is open.
* Commands: `python -m scripts.run_auto_train` with `--dry-report`, `--status` or `--rollback`.
* Output: `data/auto_train/` and `data/logs/auto_train.log`; the admin console shows it under Control.

Weekly re-runs reuse the same hold-out, so passing is weaker evidence than it looks. Nothing here guarantees profit.
