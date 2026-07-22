# Architecture & Backup Strategy

## System diagram (conceptual)

```
                    ┌─────────────────────────────┐
                    │   LOCAL MACHINE (watchdog)   │
                    │  scripts/heartbeat_monitor.py│
                    │  - polls VPS /health          │
                    │  - independent alert channel  │
                    │  - pulls nightly backups       │
                    └───────────┬───────────────────┘
                                │ polls every 60s
                                ▼
                    ┌─────────────────────────────┐
                    │        VPS (primary)          │
                    │                                │
                    │  main.py (trading engine)      │
                    │   ├─ data ingestion (ccxt/OANDA)│
                    │   ├─ strategy/signal engine     │
                    │   ├─ risk_manager (gatekeeper)  │
                    │   └─ execution_manager (orders) │
                    │                                │
                    │  dashboard/app.py (FastAPI)     │
                    │   ├─ /health                    │
                    │   ├─ /api/status                │
                    │   └─ /api/kill-switch, /resume   │
                    │                                │
                    │  data/state.db (SQLite, WAL)    │
                    │  data/backups/ (cron, 6h)       │
                    └───────────┬───────────────────┘
                                │ REST orders + stop-loss orders
                                ▼
                    ┌─────────────────────────────┐
                    │  Exchange / Broker            │
                    │  (Binance / Alpaca / OANDA)   │
                    │  - holds YOUR stop-loss orders │
                    │  - trade-only API key           │
                    └─────────────────────────────┘

     STAKE WALLET (separate account/cold storage — bot has NO access, ever)
```

## Why two machines, not one
A bot running solely on your laptop stops trading the moment you close the lid,
lose wifi, or your ISP hiccups — mid-position, with no one watching. A VPS runs
24/7 with much better uptime than a home connection. But a VPS can also go down
(provider outage, disk full, process crash), and if the *only* thing watching the
bot is the bot itself, a VPS-wide outage means nobody gets told. Two independent
machines, each capable of alerting you on its own, closes that gap. Neither one
alone is "safe" — the exchange-side stop-loss orders (Mistake #2 in
COMMON_MISTAKES.md) are what actually protect an open position when *both*
machines are down at once.

## Backup strategy (3-2-1 rule)
- **3 copies** of trade state: live DB on the VPS, local backup folder on the VPS,
  and synced copy on your local machine (optionally a 4th in cloud object storage).
- **2 different media/locations**: VPS disk + your local machine (physically
  separate infrastructure/provider).
- **1 offsite**: your local machine (or cloud storage) counts as offsite relative
  to the VPS provider.

**What's backed up**: `data/state.db` (all trades, open positions, risk counters),
`config/config.yaml` (so you can restore exact risk settings), `.env` should be
backed up manually and separately, encrypted — never synced in plaintext.

**Frequency**: every 6 hours via cron (`scripts/backup.sh`), plus an automatic
snapshot after every single trade (`state_snapshot.json`, near-real-time).

**Restore procedure**:
```bash
sudo systemctl stop tradingbot
cp data/backups/state_<timestamp>.db data/state.db
sudo systemctl start tradingbot
# verify /api/status shows the expected trading_balance and consecutive_losses
```

## Database: SQLite vs PostgreSQL/TimescaleDB
The original plan called for PostgreSQL or TimescaleDB. This build uses SQLite
instead — worth being upfront about the tradeoff rather than silently picking one:

- **SQLite (what's here)**: zero setup, single file, WAL mode gives crash-safe
  writes, trivial to back up (just copy the file). Genuinely sufficient for one
  person running a bot against a handful of symbols — the risk-state writes here
  are maybe a few per minute, nowhere near SQLite's ceiling.
- **PostgreSQL/TimescaleDB**: worth migrating to if you (a) start storing your own
  tick/candle history at scale for backtesting instead of re-fetching from the
  exchange each time, (b) run multiple bot instances against the same database
  concurrently, or (c) want proper time-series query performance for analytics
  across months of data.

If you outgrow SQLite, the `StateManager` class is the only place that would need
rewriting (swap the `sqlite3` calls for `sqlalchemy` + a Postgres connection) —
`risk_manager.py` and `execution_manager.py` only ever call its methods, never
raw SQL, so the migration is contained to one file.

## Security checklist
- [ ] Exchange API keys: trade-only, no withdrawal permission
- [ ] API keys IP-whitelisted to the VPS's static IP
- [ ] `.env` file `chmod 600`, never committed to git (`.gitignore` it)
- [ ] For production/serious capital: migrate secrets from `.env` to a proper
      secrets manager (AWS Secrets Manager, HashiCorp Vault, or even your VPS
      provider's built-in secrets store) — `.env` is fine to start, but a flat
      file on disk is a weaker guarantee than a secrets manager with access
      logging and rotation support once real money is at stake
- [ ] Dashboard port firewalled to your home/office IP only
- [ ] Dashboard secret key is long and random, not the default in `.env.example`
- [ ] SSH access to VPS via key only, password auth disabled
- [ ] Stake wallet is a fully separate account/address the trading API key cannot touch
- [ ] 2FA enabled on every exchange/broker account
- [ ] Regular (weekly) manual review of trade logs, not just automated alerts
