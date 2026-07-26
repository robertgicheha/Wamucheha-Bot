# Trading Bot — Multi-Asset Stake-Protected Compounding System

A risk-first algorithmic trading system across crypto (Binance, OKX), forex/gold
(OANDA), US stocks/ETFs (Alpaca), and Kenyan stocks (NSE alerts), with a
non-predictive equity screener for long-term investing.

## Honesty first
No bot can guarantee profit or "tell you which stock makes you richest." This system
is built to (a) protect your capital aggressively, (b) fail loudly and safely rather
than silently, and (c) give you evidence-based alerts for long-term investing — not
predictions dressed up as certainty. Backtest and paper-trade for months before real
money touches it.

## Supported exchanges & brokers

| Exchange | Asset Class | Automated Trading | Paper Trading |
|----------|------------|-------------------|---------------|
| Binance | Crypto (BTC, ETH, SOL, etc.) | Yes | Dry-run mode |
| OKX | Crypto (BTC, ETH, SOL, etc.) | Yes | Dry-run mode |
| OANDA | Forex (EUR/USD, GBP/USD), Gold (XAU/USD) | Yes | Practice account |
| Alpaca | US Stocks & ETFs (AAPL, SPY, GLD, QQQ) | Yes | Paper trading built-in |
| NSE Kenya | Kenyan Stocks (SCOM, EQTY, KCB) | Alerts only | N/A |

---

## Quick start — local laptop (development/testing)

### 1. Install
```bash
git clone <your-repo-url> trading_bot
cd trading_bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
```

Edit `.env` with your API keys. At minimum, fill in:
- `BINANCE_API_KEY` + `BINANCE_API_SECRET` (for crypto)
- `STAKE_AMOUNT=10` (start small for testing)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (for alerts)

### 3. Set your stake amount
In `.env`:
```
STAKE_AMOUNT=10      # $10 stake — only trade with profits
STAKE_AMOUNT=100     # $100 stake
STAKE_AMOUNT=500     # $500 stake
```
This overrides `config.yaml` without editing code. The stake is capital
that is NEVER traded — only profits from trading are risked.

### 4. Run the bot
```bash
python main.py
```

### 5. Run the dashboard (separate terminal)
```bash
uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000 in your browser.

### 6. Run the long-term screener (separate terminal)
```bash
python long_term/scheduler.py
```

### 7. Run the watchdog (separate terminal, monitors the bot)
```bash
# First, set VPS_HEARTBEAT_URL in .env (for local testing, point at localhost)
# VPS_HEARTBEAT_URL=http://127.0.0.1:8000/health
python scripts/heartbeat_monitor.py
```

---

## Deploy to VPS (production, 24/7)

### Part 1: VPS setup

Pick a VPS close to your exchange's servers for lower latency. A $5-10/month
VPS (DigitalOcean, Linode, Hetzner, AWS EC2 t3.micro) is plenty.

```bash
# On the VPS — create a dedicated user
sudo adduser tradingbot --disabled-password
sudo mkdir -p /opt/trading_bot
sudo chown tradingbot:tradingbot /opt/trading_bot

# Copy the project to VPS (from your local machine)
scp -r /path/to/trading_bot/* tradingbot@YOUR_VPS_IP:/opt/trading_bot/
# OR clone from git:
# su - tradingbot
# cd /opt/trading_bot && git clone <repo-url> .

# Install dependencies
su - tradingbot
cd /opt/trading_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
chmod 600 .env
nano .env    # fill in ALL your real API keys
```

### Part 2: Edit config.yaml

Set which exchanges and markets to trade:
```yaml
execution:
  exchanges:
    - name: binance
      enabled: true
      markets: ["BTC/USDT", "ETH/USDT"]
    - name: okx
      enabled: true
      markets: ["BTC/USDT", "ETH/USDT"]
    - name: oanda
      enabled: false
      markets: []
    - name: alpaca
      enabled: false
      markets: []
  oanda_markets: ["EUR/USD", "XAU/USD"]
  alpaca_markets: ["AAPL", "SPY", "GLD", "QQQ"]
```

### Part 3: Install systemd services

```bash
sudo cp /opt/trading_bot/deploy/tradingbot.service /etc/systemd/system/
sudo cp /opt/trading_bot/deploy/tradingbot-dashboard.service /etc/systemd/system/
sudo cp /opt/trading_bot/deploy/tradingbot-screener.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot tradingbot-dashboard tradingbot-screener
sudo systemctl status tradingbot
```

### Part 4: Firewall

Only open the dashboard port to YOUR IP, not the world:
```bash
sudo ufw allow from YOUR_HOME_IP to any port 8000
sudo ufw enable
```

### Part 5: Backups

```bash
crontab -e
# Add this line — backs up trade state every 6 hours:
0 */6 * * * /opt/trading_bot/scripts/backup.sh >> /opt/trading_bot/logs/backup.log 2>&1
```

---

## Host on local laptop as backup server

If the VPS is wiped or goes down, your local laptop can take over.
This also serves as an independent backup of trade state.

### Setup (one-time)
```bash
# On your LOCAL laptop (same repo checked out)
cd trading_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env    # fill in the same API keys as the VPS
```

### Pull latest backup from VPS
```bash
# Set up SSH key auth (one-time)
ssh-keygen -t ed25519 -f ~/.ssh/tradingbot_backup
ssh-copy-id -i ~/.ssh/tradingbot_backup.pub tradingbot@YOUR_VPS_IP

# Pull the latest state backup
scp tradingbot@YOUR_VPS_IP:/opt/trading_bot/data/backups/state_latest.db ./data/state.db
```

### Run as backup
```bash
# Option A: Run the bot in dry-run mode as a standby
python main.py    # dry_run=True by default — won't execute real trades

# Option B: Run only the watchdog to monitor the VPS
python scripts/heartbeat_monitor.py
```

### Automate backup sync (cron)
```bash
# Add to crontab — pulls state backup from VPS every hour
0 * * * * scp tradingbot@YOUR_VPS_IP:/opt/trading_bot/data/backups/*.db /path/to/trading_bot/data/backups/ 2>/dev/null
```

If the VPS dies: stop the watchdog, change `dry_run=True` to `dry_run=False`
in main.py (or the relevant executor), and your local laptop becomes the
primary trading server. When the VPS comes back, reverse the process.

---

## Link Telegram alerts

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts, get your bot token
3. Get your chat ID:
   - Message your new bot anything
   - Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   - Find `"chat":{"id":123456789}` — that's your chat ID
4. In `.env`:
```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

You'll get alerts for: trade opened/closed, circuit breakers, daily PnL,
profit sweeps, NSE analysis, heartbeat misses, and an hourly transaction/PnL
report (every trade closed in the last hour + running account totals).

### Control the bot from Telegram/Discord (not just alerts)

The steps above only make the bot able to *talk to you*. To *control* it —
check status, see profit/loss, view past hourly logs, and kill/resume/restart —
run the separate interactive control bots:

```bash
python control/telegram_bot.py     # polls Telegram for commands
python control/discord_bot.py      # real Discord bot, not the webhook above
```

Commands: `/status /positions /profit /trades /logs [n] /kill /resume /restart /refresh`

Both fail closed: nobody can issue a command until you list their user ID in
`TELEGRAM_ALLOWED_USER_IDS` / `DISCORD_ALLOWED_USER_IDS` in `.env`. Full setup
(bot tokens, intents, the sudoers rule `/restart` needs) is in
`deploy/DEPLOYMENT.md` Part 1b. For 24/7 use, install them as systemd services
just like the engine — units are in `deploy/tradingbot-telegram.service` and
`deploy/tradingbot-discord.service`.

## Link Discord alerts

1. Go to your Discord server → Settings → Integrations → Webhooks
2. Click "New Webhook", name it, pick a channel, copy the webhook URL
3. In `.env`:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abc...
```

## Link Email alerts

Email alerts only fire for HIGH-priority events (circuit breakers, heartbeat
misses) to avoid inbox fatigue. For Gmail:
1. Enable 2FA on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate an app password for "Mail"
4. In `.env`:
```
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_ADDRESS=you@gmail.com
EMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
EMAIL_TO=you@gmail.com
```

## Dashboard

The dashboard is a web UI at `http://YOUR_IP:8000` showing:
- Current risk state (balance, consecutive losses, daily PnL)
- Open positions
- Recent events log
- Kill switch / resume buttons (require dashboard secret key)

Set `DASHBOARD_SECRET_KEY` in `.env` to a long random string.

---

## Train ML models (optional)

The LSTM predictor is an optional second opinion on top of the rule-based
strategy. It can only reduce trades, never add them.

```bash
# Train on BTC/USDT 15-minute candles
python ml/train_lstm.py --exchange binance --symbol BTC/USDT --timeframe 15m --limit 3000

# Train on ETH/USDT
python ml/train_lstm.py --exchange binance --symbol ETH/USDT --timeframe 15m --limit 3000

# Train on OKX
python ml/train_lstm.py --exchange okx --symbol BTC/USDT --timeframe 15m --limit 3000
```

Check the printed test accuracy against the naive baseline. If the model
barely beats it (within 2%), it has no real edge — don't use it as a filter.

## Backtest before going live

```bash
# Backtest BTC/USDT on Binance
python scripts/run_backtest.py --exchange binance --symbol BTC/USDT --timeframe 15m --limit 1000

# Backtest on OKX
python scripts/run_backtest.py --exchange okx --symbol BTC/USDT --timeframe 15m --limit 1000

# Walk-forward backtest (train/test split — trust the "test" numbers)
# The script automatically runs both full-period and walk-forward splits.
```

---

## Docker deployment (alternative)

```bash
# Build and start all 4 services
docker compose up -d --build

# Check status
docker compose ps
docker compose logs -f engine

# Stop everything
docker compose down
```

Services:
- `engine` — main trading loop
- `dashboard` — FastAPI web UI
- `screener` — weekly long-term equity analysis
- `watchdog` — monitors the engine (ideally runs on a separate machine)

---

## Architecture (4 separate services)

```
┌─────────────────────────────────────────────────────────┐
│                    VPS (primary)                         │
│                                                         │
│  Service 1: Engine (main.py)                            │
│   ├─ Data Ingestion: Binance/OKX (ccxt), OANDA,        │
│   │   Alpaca, NSE scraper                               │
│   ├─ Strategy: EMA crossover + RSI + volume + LSTM      │
│   ├─ Risk Manager: circuit breakers, position sizing    │
│   └─ Execution: broker-specific order placement         │
│                                                         │
│  Service 2: Dashboard (dashboard/app.py)                │
│   ├─ /health, /api/status                               │
│   └─ /api/kill-switch, /api/resume                      │
│                                                         │
│  Service 3: Screener (long_term/scheduler.py)           │
│   └─ Weekly fundamentals + news sentiment analysis      │
│                                                         │
│  Service 4: Watchdog (scripts/heartbeat_monitor.py)     │
│   └─ Independent health monitor                         │
│                                                         │
│  Alerts: Telegram + Discord + Email (all channels)      │
│  State: SQLite with WAL mode (crash-safe)               │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│           LOCAL LAPTOP (backup + watchdog)               │
│  - Polls VPS /health every 60s                          │
│  - Independent alert channel if VPS dies                │
│  - Can take over trading if VPS is wiped                │
│  - Pulls nightly state backups from VPS                 │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│              STAKE WALLET (cold storage)                 │
│  - Bot has NO access, ever                              │
│  - Only receives profit sweeps from trading balance     │
│  - Set STAKE_AMOUNT in .env (e.g. 10, 100, 500)        │
└─────────────────────────────────────────────────────────┘
```

## Going live — the one switch that matters

`LIVE_TRADING=false` in `.env` (the default) means every exchange/broker
executor runs in dry-run: signals still fire, trades are still logged and
scored, but no real order ever reaches Binance/OKX/OANDA/Alpaca. Set
`LIVE_TRADING=true` only after you're satisfied with weeks of dry-run/paper
results — this is the only place that decides real-vs-simulated, on purpose,
so it's one clearly auditable line instead of scattered across executor code.

## How the bot actually buys and sells

**Entry** (`strategy/technical_strategy.generate_signal`): EMA(9)/EMA(21)
bullish crossover, confirmed by RSI between 45–70 (momentum without being
overbought) and above-average volume (real participation, not a thin tick).
An optional LSTM model can only make this MORE conservative — it can veto a
signal, never create one on its own.

**Exit** (`core/position_monitor.py` + `strategy/technical_strategy.generate_exit_signal`),
checked every ~15 seconds against every open position, in this order:
1. Fixed stop-loss / take-profit — the floor, also placed as a real
   exchange-side order at entry in live mode so it protects you even if the
   bot process dies.
2. Trend reversal — EMA(9) crosses back below EMA(21): the move that
   justified the entry is over.
3. RSI exhaustion — RSI pushes above 80 after a run: momentum is stretched,
   take the win.
4. Trailing stop — once a position is up `trailing_stop_activate_pct`
   (config.yaml), a stop trails `trailing_stop_distance_pct` behind the peak
   price so a winner can't fully round-trip back to breakeven.

None of this guarantees profit. It's a disciplined, debuggable rule set with
a real exit path — not a claim that it beats the market.

## Risk rules

- **Stake protection**: `STAKE_AMOUNT` (set in `.env`) is never traded
- **Position sizing**: max 2% of trading balance per trade
- **8-loss circuit breaker**: halts ALL trading after 8 consecutive losses
- **Daily loss limit**: stops trading if 15% of balance lost in a day
- **Volatility breaker**: pauses if price moves 5%+ in 5 minutes
- **Profit sweep**: when balance hits $500, sweeps $300 to stake wallet
- **Stop-loss**: placed as real exchange-side order at trade entry, not just in-memory
- **NSE**: alerts/analysis only — no automated execution (no public trading API)

## Read before going live
- `docs/COMMON_MISTAKES.md` — real failure modes and how this system avoids them
- `docs/ARCHITECTURE.md` — full system design, backup strategy, failover

## Not financial advice
This is engineering scaffolding, not investment advice. You are responsible for
regulatory compliance in your jurisdiction (in Kenya: Capital Markets Authority
rules on algorithmic/forex trading apply).