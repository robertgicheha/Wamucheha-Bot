# Architecture & Backup Strategy

## System diagram

```
                    ┌──────────────────────────────────────┐
                    │   LOCAL MACHINE (watchdog + backup)   │
                    │  scripts/heartbeat_monitor.py         │
                    │  - polls VPS /health every 60s        │
                    │  - independent alert channel          │
                    │  - can take over if VPS dies          │
                    └───────────┬──────────────────────────┘
                                │ polls every 60s
                                ▼
                    ┌──────────────────────────────────────┐
                    │            VPS (primary)               │
                    │                                       │
                    │  main.py (trading engine)              │
                    │   ├─ data ingestion:                   │
                    │   │   ccxt (Binance, OKX, Kraken)     │
                    │   │   OANDA (forex, XAU/USD)          │
                    │   │   Alpaca (US stocks, ETFs)         │
                    │   │   NSE scraper (Kenyan stocks)      │
                    │   ├─ strategy: EMA/RSI + LSTM filter   │
                    │   ├─ risk_manager (gatekeeper)         │
                    │   ├─ execution (broker-specific)       │
                    │   └─ NSE analysis loop (hourly)        │
                    │                                       │
                    │  dashboard/app.py (FastAPI)            │
                    │   ├─ /health                           │
                    │   ├─ /api/status                       │
                    │   └─ /api/kill-switch, /resume         │
                    │                                       │
                    │  long_term/scheduler.py (screener)     │
                    │   └─ weekly fundamentals + news        │
                    │                                       │
                    │  data/state.db (SQLite, WAL)           │
                    │  data/backups/ (cron, 6h)              │
                    └───────────┬──────────────────────────┘
                                │ REST orders + stop-loss orders
                                ▼
                    ┌──────────────────────────────────────┐
                    │  Exchanges / Brokers                   │
                    │  Binance | OKX | OANDA | Alpaca        │
                    │  - holds YOUR stop-loss orders         │
                    │  - trade-only API keys                 │
                    └──────────────────────────────────────┘

     STAKE WALLET (separate account/cold storage)
     - Amount set via STAKE_AMOUNT in .env (default: $100)
     - Bot has NO access, ever
     - Only receives profit sweeps
```

## The 4 services (not one monolith)

| Service | Process | Purpose |
|---------|---------|---------|
| Engine | `python main.py` | Data ingestion, strategy, risk gating, execution, NSE analysis |
| Dashboard | `uvicorn dashboard.app:app` | Web UI, kill switch, status API |
| Screener | `python long_term/scheduler.py` | Weekly long-term equity analysis + alerts |
| Watchdog | `python scripts/heartbeat_monitor.py` | Independent VPS health monitor |

Keeping execution logic separate from strategy logic is the single biggest thing
that prevents catastrophic bugs — a bad signal cannot bypass risk limits.

## Stake amount configuration

The stake amount (capital that is NEVER traded) is configurable two ways:

1. **Environment variable** (preferred): Set `STAKE_AMOUNT=10` in `.env`
   - Overrides config.yaml at startup
   - Change without editing any code: just edit `.env` and restart
2. **Config file**: Edit `config/config.yaml` -> `account.stake_amount`

Examples:
- `STAKE_AMOUNT=10` — start with $10, compound profits
- `STAKE_AMOUNT=100` — default, $100 stake
- `STAKE_AMOUNT=500` — larger stake for more margin

## Exchanges supported

| Exchange | Type | ccxt | Passphrase | Notes |
|----------|------|------|------------|-------|
| Binance | Crypto | Yes | No | Primary crypto exchange |
| OKX | Crypto | Yes | Yes | Requires API passphrase |
| Kraken | Crypto | Yes | No | Alternative to Binance |
| Coinbase | Crypto | Yes | No | US-regulated |
| Bybit | Crypto | Yes | No | Derivatives focus |
| OANDA | Forex/Gold | No (direct API) | No | Practice account available |
| Alpaca | Stocks/ETFs | No (direct API) | No | Paper trading built-in |
| NSE Kenya | Kenyan stocks | No (scraper) | N/A | Alerts only, no execution |

## NSE (Nairobi Securities Exchange) handling

NSE does NOT offer a public trading API. No ccxt support, no broker with open
algo-access. The system handles this by:

1. **Data**: Scrapes NSE daily OHLCV via Apify or web scraper, cached locally
2. **Analysis**: Runs hourly technical analysis (200DMA, golden cross, momentum)
3. **News sentiment**: Pulls headlines via NewsAPI, scores with VADER
4. **Alerts**: Sends analysis to ALL channels (Telegram, Discord, email, dashboard)
5. **No execution**: Any actual NSE trade must go through your broker manually
   (Genghis Capital, AIB-AXYS, Faida Investment Bank)

NSE alert event type: `nse_alert`

## Why two machines, not one

A bot running solely on your laptop stops trading the moment you close the lid,
lose wifi, or your ISP hiccups — mid-position, with no one watching. A VPS runs
24/7 with much better uptime. But a VPS can also go down (provider outage, disk
full, process crash), and if the *only* thing watching the bot is the bot itself,
a VPS-wide outage means nobody gets told. Two independent machines, each capable
of alerting you on its own, closes that gap.

The exchange-side stop-loss orders are what actually protect an open position when
*both* machines are down at once.

## Backup strategy (3-2-1 rule)

- **3 copies** of trade state: live DB on VPS, local backup folder on VPS,
  synced copy on your local machine.
- **2 different media/locations**: VPS disk + your local machine.
- **1 offsite**: your local machine (or cloud storage).

**What's backed up**: `data/state.db` (all trades, open positions, risk counters),
`config/config.yaml` (exact risk settings). `.env` should be backed up manually
and encrypted — never synced in plaintext.

**Frequency**: every 6 hours via cron (`scripts/backup.sh`), plus automatic
snapshot after every trade (`state_snapshot.json`).

**Restore procedure**:
```bash
sudo systemctl stop tradingbot
cp data/backups/state_<timestamp>.db data/state.db
sudo systemctl start tradingbot
# verify /api/status shows expected trading_balance and consecutive_losses
```

## Local laptop as backup server

If the VPS is wiped or goes down, your local laptop can take over:

```bash
# Pull latest state backup from VPS
scp tradingbot@YOUR_VPS_IP:/opt/trading_bot/data/backups/*.db ./data/backups/

# Restore it
cp data/backups/state_latest.db data/state.db

# Start the bot (dry_run=True by default — safe)
python main.py

# When ready to take over live trading, edit the executors:
# Change dry_run=True to dry_run=False in main.py for each executor
```

## Database: SQLite vs PostgreSQL/TimescaleDB

SQLite with WAL mode is used here — zero setup, single file, crash-safe writes,
trivial to back up. Sufficient for one person running a bot against a handful of
symbols.

Migrate to PostgreSQL/TimescaleDB if you (a) store your own tick/candle history
at scale, (b) run multiple bot instances concurrently, or (c) want proper
time-series query performance across months of data.

## Security checklist

- [ ] Exchange API keys: trade-only, NO withdrawal permission
- [ ] API keys IP-whitelisted to VPS static IP
- [ ] `.env` file `chmod 600`, never committed to git
- [ ] For production: migrate secrets from `.env` to a secrets manager
- [ ] Dashboard port firewalled to your IP only
- [ ] `DASHBOARD_SECRET_KEY` is long and random
- [ ] SSH access via key only, password auth disabled
- [ ] Stake wallet is a fully separate account/address
- [ ] 2FA enabled on every exchange/broker account
- [ ] OKX passphrase stored securely (not in version control)
- [ ] Regular (weekly) manual review of trade logs
