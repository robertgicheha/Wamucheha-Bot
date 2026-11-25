# Trading Bot: Crypto + Gold, Stake-Protected Compounding

A risk-first algorithmic trading system for **crypto** (OKX, Binance, Bybit via ccxt)
and **gold** (spot XAUUSD via MetaTrader 5, plus the tokenised PAXG/USDT pair on OKX),
with a browser admin console (`/admin`) to start, pause, kill, dry-test and monitor it.

## Honesty first
No bot can guarantee profit or "tell you which stock makes you richest." This system
is built to (a) protect your capital aggressively, (b) fail loudly and safely rather
than silently, and (c) show you honest, evidence-based performance numbers, not
predictions dressed up as certainty. Backtest and paper-trade for months before real
money touches it.

## Supported markets

| Venue | Markets | Automated trading | Paper trading |
|-------|---------|-------------------|---------------|
| OKX (ccxt) | BTC/USDT, ETH/USDT, PAXG/USDT (gold token) | Yes | Dry-run mode (works in Docker, no API keys needed for data) |
| Binance, Bybit (ccxt) | Crypto | Yes (disabled by default) | Dry-run mode |
| MetaTrader 5 | XAUUSD (spot gold) | Yes | Demo account + dry-run. **Windows only** (the MT5 terminal does not run in Linux containers) |

---

## Quick start — local laptop (development/testing)

### 1. Install
```bash
git clone <your-repo-url> trading_bot
cd trading_bot
python3 -m venv .venv
source .venv/bin/activate       # Linux/macOS (bash)
pip install -r requirements.txt
```

Windows PowerShell (`source` does not exist there, and the folder is `.venv`).
Run `python -m venv .venv` **only once**; it fails with "Permission denied" if the venv is active or in use.
Next time just activate it:
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env            # PowerShell: Copy-Item .env.example .env
```

Edit `.env`. At minimum, fill in:
- `LIVE_TRADING=false` (keep it `false` until `python scripts/paper_readiness.py` says READY)
- `DASHBOARD_SECRET_KEY=<long random string>` (the admin console's control buttons stay disabled while it is `change_me`)
- `STAKE_AMOUNT` (start small for testing)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (for alerts)
- Crypto venues (only if enabled in `config/config.yaml`): `BINANCE_API_KEY` + `BINANCE_API_SECRET`, etc.
- **MT5 (the default venue, trades XAUUSD):** `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`.
  Leave `MT5_RPC_HOST` empty on Windows. If several MT5 terminals are installed, set
  `MT5_TERMINAL_PATH` to the one you use.

**MT5 prerequisites (Windows):** install the MetaTrader 5 desktop app, open a **demo** account in it
(File -> Open an Account) and confirm it connects (green bars, bottom right) *before* starting the bot.
The `MetaTrader5` Python package only works on Windows, so the engine must run natively on Windows
(not inside a Linux container).

### Fund your exchange accounts (manual, one-time)
The bot **never moves money**. Funding is **one funder, one settlement, no
wallet**: capital is drawn from one exchange (OKX) and every venue's profit is
withdrawn back to it. There is no MetaMask, no self-custody wallet, and no
seed phrase anywhere in the design — you perform every transfer yourself in the
exchange UIs.
Note that cross-venue funding is an on-chain withdrawal (exchange-internal
transfers only work within one exchange), so each hop costs a network fee and
takes settlement time.
See **[DEPOSIT_WITHDRAWAL.md](DEPOSIT_WITHDRAWAL.md)** for the exact addresses,
networks (BEP20/TRC20), fees, and step-by-step checklist.
Run `python scripts/preflight.py` to validate your setup before going live.

### 3. Set your stake amount
In `.env`:
```
STAKE_AMOUNT=10      # $10 stake — only trade with profits
STAKE_AMOUNT=100     # $100 stake
STAKE_AMOUNT=500     # $500 stake
```
This overrides `config.yaml` without editing code. The stake is capital
that is NEVER traded — only profits from trading are risked.

### 4. Check your setup, then run the bot
```bash
python scripts/preflight.py     # read every blocker/warning first
python main.py                  # keep this terminal open (Ctrl+C stops it)
```
A healthy start prints `MT5 executor initialized` and `MODE: DRY-RUN`. If it prints
`MT5 connection failed`, fix the MT5 login (see Troubleshooting) and restart.

### 5. Run the dashboard (separate terminal, venv activated)
```bash
uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```
- http://127.0.0.1:8000 : main dashboard
- http://127.0.0.1:8000/admin : admin console (performance, errors, logs, projections, halt/resume/restart). Sign in with `DASHBOARD_SECRET_KEY`.

The engine and dashboard share the `data/` folder, so start both from the project root.

### Paper-trade before going live
Follow [docs/PAPER_TRADING_CHECKLIST.md](docs/PAPER_TRADING_CHECKLIST.md). Run
`python scripts/paper_readiness.py` weekly. Only set `LIVE_TRADING=true` after it says READY.

### Troubleshooting
| Symptom | Fix |
|---|---|
| `source` / `venv\Scripts\activate` not recognised | PowerShell: `.venv\Scripts\Activate.ps1` (folder is `.venv`) |
| `MT5 initialize failed: (-6, 'Authorization failed')` | Wrong/expired MT5 login, password or server name. Log in manually in the MT5 app first |
| MT5 start hangs or picks the wrong terminal | Set `MT5_TERMINAL_PATH` in `.env` |
| `docker compose` 500 error | Docker Desktop engine not running; install WSL (`wsl --install --no-distribution`) and reboot |
| Control buttons disabled in `/admin` | Change `DASHBOARD_SECRET_KEY` from `change_me` |

### 6. Start the bot from the dashboard
Run `.\start-dashboard.ps1`, open http://127.0.0.1:8000/admin, go to **Control** and press **Start**.
That launches the engine as a background process; **Pause / Kill switch / Run dry test / Stop**
are on the same page. The bot is in DRY-RUN unless `LIVE_TRADING=true`.

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
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot tradingbot-dashboard
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
```

### Automate backup sync (cron)
```bash
# Add to crontab — pulls state backup from VPS every hour
0 * * * * scp tradingbot@YOUR_VPS_IP:/opt/trading_bot/data/backups/*.db /path/to/trading_bot/data/backups/ 2>/dev/null
```

If the VPS dies: change `dry_run=True` to `dry_run=False`
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
profit sweeps, heartbeat misses, and an hourly transaction/PnL
report (every trade closed in the last hour + running account totals).

### Controlling the bot
Control is done from the dashboard's **Control** tab (`/admin`), not from chat bots.
Telegram here is for alerts only.

## Link Discord alerts

1. Go to your Discord server → Settings → Integrations → Webhooks
2. Click "New Webhook", name it, pick a channel, copy the webhook URL
3. In `.env`:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abc...
```

## Link Email alerts

Email is reserved for the **Daily Investing & Markets Digest** only — it is the one
report meant to be read once and kept. Everything else (startup, every trade open
and close, hourly summaries, risk events, signals) goes to Telegram and Discord
only, so the inbox stays signal rather than a firehose. The allowlist lives in
`alerts/notifier.py` as `EMAIL_ALLOWED_EVENTS`; add an event type there to change it.

For Gmail:
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

## Docker deployment

```bash
docker compose up -d --build     # engine + dashboard
docker compose ps
docker compose logs -f engine
docker compose down
```

Services:
- `engine`: the trading loop. DRY-RUN unless `LIVE_TRADING=true`. Trades crypto and the PAXG/USDT gold token.
- `dashboard`: the admin console at http://127.0.0.1:8000/admin (`DASHBOARD_PORT` in `.env` changes the host port).

**Gold via MT5 is not available in Docker** (MT5 is a Windows program). To trade XAUUSD
through MT5, run the engine natively on Windows (dashboard Start button, or `python main.py`).
In Docker the **Start** button does nothing useful because the engine container is always
supervised; use **Pause/Resume** or **Standby** there.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Engine (main.py)                                        │
│   ├─ Data: OKX/Binance/Bybit (ccxt), MT5 (gold)          │
│   ├─ Strategy: 20-strategy ensemble + regime filter      │
│   ├─ Risk Manager: circuit breakers, position sizing     │
│   └─ Execution: ccxt orders / MT5 orders (dry-run first) │
│                                                          │
│  Dashboard (dashboard/app.py, /admin)                    │
│   ├─ KPIs, trade log, candlestick charts, analytics      │
│   └─ Control: start, pause, kill switch, dry test, stop  │
│                                                          │
│  Alerts (optional): Telegram / email                     │
│  State: SQLite with WAL mode (crash-safe)                │
└──────────────────────────────────────────────────────────┘
```

## Going live — the one switch that matters

`LIVE_TRADING=false` in `.env` (the default) means every exchange/broker
executor runs in dry-run: signals still fire, trades are still logged and
scored, but no real order ever reaches OKX/Binance/Bybit/MT5. Set
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

## Read before going live
- `docs/COMMON_MISTAKES.md` — real failure modes and how this system avoids them
- `docs/ARCHITECTURE.md` — full system design, backup strategy, failover

## Not financial advice
This is engineering scaffolding, not investment advice. You are responsible for
regulatory compliance in your jurisdiction (in Kenya: Capital Markets Authority
rules on algorithmic/forex trading apply).
---

## Admin console

Open `http://<host>:8000/admin` and sign in with `DASHBOARD_SECRET_KEY`. Tabs:
Overview (mode banner, money cards: starting capital / total after trades / paid out /
total value, performance cards: gross profit, gross loss, win and loss rate,
efficiency, profit factor, expectancy, drawdown, plus equity curve, positions and a
live trade log), Market (MT5-style candlestick chart with volume, EMA 9/21 and your
trade entries/exits; symbol, timeframe and source selectable; candles come from the
MT5 terminal, else public exchange data, else yfinance), Trades, Analysis (by symbol / strategy / regime / hour / weekday / venue),
Projection (Monte Carlo of your own trade results; flagged unreliable under 30
trades; not a market forecast), Errors, Logs, Signals & Events, and Control.

**Control actions** (start, pause, resume, kill switch, dry-run, dry test, standby,
stop, restart) are disabled until
`DASHBOARD_SECRET_KEY` is changed from `change_me`. The dashboard never touches
the trading DB or Docker: it drops a command file into the shared
`engine-control` volume (`data/control`), and the engine executes it within a
few seconds and records it in `data/control/audit.jsonl`.

- **Standby** ("shutdown") halts trading and idles the engine but keeps the
  process alive, because `restart: unless-stopped` / `Restart=always` would
  relaunch a real exit. To stop the process itself run `docker compose stop engine`.
- **Start** launches `main.py` as a detached process (native runs only; under Docker
  use `docker compose up -d engine`).
- **Kill switch** halts trading and locks it: a normal resume is refused until you type
  `release`. Optionally closes every open position at market. Typed word: `KILL`.
- **Mode**: the dashboard can force DRY-RUN but can never turn real trading ON. Going
  live needs `LIVE_TRADING=true` in `.env` and a restart.
- **Dry test** checks executor connections, candle data and strategy decisions for every
  market without placing any order; the result appears on the Control tab.
- **Restart** makes the engine exit cleanly so the supervisor relaunches it.
- Engine errors (anything printed with "error/failed/exception", plus uncaught
  exceptions) are copied to `data/logs/errors.log` (5 MB cap) for the Errors tab.
