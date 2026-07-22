#!/usr/bin/env bash
# Run via cron on the VPS, e.g.:  0 */6 * * * /path/to/backup.sh >> /var/log/tradingbot-backup.log 2>&1
#
# Strategy: 3 copies of state, in 3 places, per the standard 3-2-1 backup rule.
#   1. Local hot backup on the VPS itself (data/backups/) — fast recovery from a bad deploy.
#   2. Synced to your LOCAL machine via rsync over SSH — survives total VPS loss.
#   3. (Optional) Pushed to cloud object storage (S3/Backblaze) — survives local machine loss too.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/data/backups"
TS=$(date -u +"%Y%m%dT%H%M%SZ")

mkdir -p "$BACKUP_DIR"

# 1. Local snapshot (python handles the SQLite-safe hot backup)
python3 -c "
import sys; sys.path.insert(0, '$PROJECT_DIR')
from core.state_manager import StateManager
sm = StateManager(stake_amount=0)
path = sm.backup_now()
print(f'Backed up to {path}')
"

# Keep only the last 30 local backups
ls -1t "$BACKUP_DIR"/state_*.db 2>/dev/null | tail -n +31 | xargs -r rm --

# 2. Sync to local machine (requires SSH key auth set up in advance, no password prompts)
if [ -n "${LOCAL_MACHINE_SSH:-}" ]; then
  rsync -az "$BACKUP_DIR/" "${LOCAL_MACHINE_SSH}:~/tradingbot_backups/"
  rsync -az "$PROJECT_DIR/config/config.yaml" "${LOCAL_MACHINE_SSH}:~/tradingbot_backups/config_${TS}.yaml"
fi

# 3. Optional cloud sync (uncomment and configure if using S3-compatible storage)
# aws s3 sync "$BACKUP_DIR" s3://your-bucket/tradingbot-backups/ --only-show-errors

echo "Backup complete: $TS"
