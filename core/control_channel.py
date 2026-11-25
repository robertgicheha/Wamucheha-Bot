"""
Dashboard -> engine command mailbox.

Why this exists: the dashboard container mounts the engine's data read-only and
has no Docker access, so it cannot write the risk-state DB or restart anything.
Instead it drops a small JSON command file into a SHARED, writable directory
(data/control, its own volume) and the engine, which is already looping,
consumes it. One direction, one file per command, nothing executable: the
engine only acts on a fixed allow-list of verbs, so a tampered file can at
worst trigger one of those verbs.

Verbs:
  halt     stop opening trades (same flag the kill switch sets)
  resume   clear the halt and leave standby
  standby  "shutdown": stop trading AND stop polling markets; process stays up
           (under `restart: unless-stopped` a real exit would just be restarted)
  restart  engine exits cleanly; the container supervisor starts it again
  kill     KILL SWITCH: halt immediately and LOCK the halt (a plain resume is refused;
           release needs resume with args.release_kill). args.close_positions=True
           also closes every open position at market.
  stop     real process exit with no restart request (native runs; under a supervisor
           it will simply be relaunched)
  dry_run  force every executor into simulated mode. There is deliberately NO verb that
           turns real trading ON: going live needs LIVE_TRADING=true in .env + restart.
  dry_test one-shot self-test (connectivity, data, signals) that places no orders;
           result lands in data/control/dry_test.json
"""
import json
import os
import time
import uuid
from pathlib import Path

CONTROL_DIR = Path(os.environ.get("CONTROL_DIR",
                                  Path(__file__).parent.parent / "data" / "control"))
ALLOWED_COMMANDS = {"halt", "resume", "standby", "restart", "kill", "stop", "dry_run", "dry_test"}
MAX_COMMAND_AGE_SECONDS = 600   # a command queued while the engine was down must not
                                # fire minutes later on an unrelated boot


def _dir() -> Path:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    return CONTROL_DIR


def send_command(command: str, by: str = "dashboard", note: str = "", args: dict | None = None) -> dict:
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    payload = {"id": uuid.uuid4().hex[:12], "command": command, "by": by,
               "note": note[:200], "ts": time.time(),
               "args": {k: v for k, v in (args or {}).items() if isinstance(v, (bool, int, str))}}
    d = _dir()
    tmp = d / f".{payload['id']}.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.replace(d / f"cmd_{int(payload['ts'] * 1000)}_{payload['id']}.json")  # atomic publish
    return payload


def poll_commands() -> list:
    """Consume and return pending commands, oldest first. Stale/invalid ones are dropped."""
    out = []
    try:
        files = sorted(_dir().glob("cmd_*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text())
            f.unlink()
        except Exception:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        if data.get("command") not in ALLOWED_COMMANDS:
            continue
        if time.time() - float(data.get("ts", 0)) > MAX_COMMAND_AGE_SECONDS:
            continue
        out.append(data)
    return out


def append_audit(entry: dict):
    """Append-only record of who commanded what. Written by the engine on execution."""
    try:
        with open(_dir() / "audit.jsonl", "a") as f:
            f.write(json.dumps({**entry, "executed_at": time.time()}) + "\n")
    except OSError:
        pass


def read_audit(n: int = 50) -> list:
    p = CONTROL_DIR / "audit.jsonl"
    try:
        lines = p.read_text().splitlines()[-n:]
        return [json.loads(l) for l in reversed(lines)]
    except Exception:
        return []


def pending_count() -> int:
    try:
        return len(list(CONTROL_DIR.glob("cmd_*.json")))
    except OSError:
        return 0
