# Deployment Guide

## Part 1 — VPS setup (primary execution)

Choose a VPS close to your exchange's servers for lower latency (e.g. a provider
with a Tokyo/Singapore region if trading on a Binance endpoint, or US-East for
Alpaca). A $5-10/month VPS (DigitalOcean, Linode, Hetzner) is plenty to start.

```bash
# On the VPS
sudo adduser tradingbot --disabled-password
sudo mkdir -p /opt/trading_bot
sudo chown tradingbot:tradingbot /opt/trading_bot

# copy your project there (scp, git clone, or rsync)
su - tradingbot
cd /opt/trading_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real keys, chmod 600 .env
```

Install the systemd services:
```bash
sudo cp deploy/tradingbot.service /etc/systemd/system/
sudo cp deploy/tradingbot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot tradingbot-dashboard
sudo systemctl status tradingbot
```

**Firewall**: only open the dashboard port (8000) to your own IP, not the world.
```bash
sudo ufw allow from YOUR_HOME_IP to any port 8000
sudo ufw enable
```

## Part 1b — Interactive Telegram/Discord control bots

These are separate long-running processes from the alert channels — the alerts
in `.env` (`TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`) only
ever push notifications out. To actually type `/status`, `/kill`, `/restart`
etc. from your phone, you need these running too:

```bash
sudo cp deploy/tradingbot-telegram.service /etc/systemd/system/
sudo cp deploy/tradingbot-discord.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-telegram tradingbot-discord
```

Before starting them, fill in `.env`:
- `TELEGRAM_ALLOWED_USER_IDS` — your numeric Telegram user ID (message
  [@userinfobot](https://t.me/userinfobot) to get it), comma-separated if more
  than one person should have control. **Leave this blank and the bot refuses
  every command from everyone** — it fails closed on purpose since these
  commands can halt/resume/restart something trading real money.
- `DISCORD_BOT_TOKEN` — a real bot token from the
  [Discord Developer Portal](https://discord.com/developers/applications)
  (New Application → Bot → Reset Token). This is different from
  `DISCORD_WEBHOOK_URL`; a webhook can't receive commands, only a real bot can.
  Enable "Message Content Intent" under Bot settings, then invite it to your
  server via OAuth2 → URL Generator (scope: `bot`, permissions: Send Messages
  + Read Message History).
- `DISCORD_ALLOWED_USER_IDS` — same fail-closed logic as Telegram. Get your
  Discord user ID via User Settings → Advanced → Enable Developer Mode, then
  right-click your name → Copy User ID.
- `DISCORD_CONTROL_CHANNEL_ID` (optional) — restrict commands to one channel.

**`/restart` needs one narrowly-scoped sudo rule.** The `tradingbot` OS user
normally can't restart systemd services. Grant it permission for exactly this
command and nothing else:
```bash
sudo visudo -f /etc/sudoers.d/tradingbot-restart
```
Add this single line (adjust the path to `systemctl` if `which systemctl`
differs on your distro):
```
tradingbot ALL=(root) NOPASSWD: /bin/systemctl restart tradingbot tradingbot-dashboard tradingbot-screener
```
Save, then `sudo chmod 440 /etc/sudoers.d/tradingbot-restart`. This is
deliberately the *only* elevated permission the control bots have — they
cannot run arbitrary shell commands, only this exact restart line.

Test it from Telegram/Discord with `/status` first (read-only, safe), then
`/kill` + `/resume` (auth-gated by `DASHBOARD_SECRET_KEY` through the
dashboard API), then finally `/restart` once you trust the sudoers rule works.

**Backups via cron** (run as the tradingbot user):
```bash
crontab -e
# add:
0 */6 * * * /opt/trading_bot/scripts/backup.sh >> /opt/trading_bot/logs/backup.log 2>&1
```

## Part 2 — Local machine setup (watchdog, independent of the VPS)

This is the piece that catches "the VPS itself died" — which the VPS obviously
can't report on its own.

```bash
# On your local machine, same repo checked out
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# set VPS_HEARTBEAT_URL=http://YOUR_VPS_IP:8000/health
python scripts/heartbeat_monitor.py
```

Keep this running permanently — as a background process, a `launchd`/Task
Scheduler job, or in a terminal multiplexer (tmux/screen) so it survives you
closing your laptop lid... though note a sleeping laptop can't poll either.
For true 24/7 watchdog coverage, a second cheap VPS in a different provider/region
watching the first is more reliable than a personal machine — a personal machine is
a fine start but has the same "what if it's offline" problem you're trying to solve.

Also set up SSH key auth from VPS → local machine (or a second backup target) so
`scripts/backup.sh` can rsync without a password prompt:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/tradingbot_backup
ssh-copy-id -i ~/.ssh/tradingbot_backup.pub you@your-local-ip   # or use a dyndns/cloud target
```

## Part 3 — Verify before any real money

1. Set `ALPACA_PAPER=true` and leave `LIVE_TRADING=false` (or unset) in `.env` —
   this is now the single switch that controls dry-run vs. real orders across
   every exchange/broker; you no longer need to edit any code.
2. Run for at least a few weeks watching Telegram/Discord alerts fire correctly.
3. Manually kill the VPS process (`sudo systemctl stop tradingbot`) and confirm the
   watchdog alerts you within `HEARTBEAT_ALERT_AFTER_MINUTES`.
4. Manually trigger the circuit breaker (force 8 losing dry-run trades) and confirm
   trading halts and does NOT auto-resume.
5. Confirm a restart of the VPS process preserves `consecutive_losses` and
   `trading_balance` correctly (this is the state-persistence test — the most
   important one).
6. Only then flip to live keys, starting with the smallest position sizes.