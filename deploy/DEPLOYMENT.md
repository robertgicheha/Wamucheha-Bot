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

1. Set `ALPACA_PAPER=true` and `dry_run=True` in the execution manager.
2. Run for at least a few weeks watching Telegram/Discord alerts fire correctly.
3. Manually kill the VPS process (`sudo systemctl stop tradingbot`) and confirm the
   watchdog alerts you within `HEARTBEAT_ALERT_AFTER_MINUTES`.
4. Manually trigger the circuit breaker (force 8 losing dry-run trades) and confirm
   trading halts and does NOT auto-resume.
5. Confirm a restart of the VPS process preserves `consecutive_losses` and
   `trading_balance` correctly (this is the state-persistence test — the most
   important one).
6. Only then flip to live keys, starting with the smallest position sizes.
