"""
Process control for the FX trend bot, used by the admin dashboard.

The bot is launched as a DETACHED process, so restarting or crashing the
dashboard never takes the bot down with it. Control is file-based:
  data/fx_trend.pid    written by the bot at start, removed at clean exit
  data/fx_trend.STOP   graceful stop: bot exits its loop, keeps positions open
  data/fx_trend.KILL   flatten all of this bot's positions, then exit
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


class BotSupervisor:
    def __init__(self, root: Path | str, python: str | None = None,
                 script: str = "scripts/run_fx_trend_bot.py"):
        self.root = Path(root)
        self.python = python or sys.executable
        self.script = self.root / script
        d = self.root / "data"
        self.pid_file, self.stop_file = d / "fx_trend.pid", d / "fx_trend.STOP"
        self.kill_file, self.hb_file = d / "fx_trend.KILL", d / "fx_trend_heartbeat.json"
        self.console_log = d / "logs" / "fx_trend_console.log"

    # ---- status ---------------------------------------------------------
    def pid(self) -> int:
        try:
            return int(self.pid_file.read_text().strip())
        except Exception:
            return 0

    def running(self) -> bool:
        pid = self.pid()
        return pid_alive(pid)

    def status(self) -> dict:
        hb, age = {}, None
        try:
            hb = json.loads(self.hb_file.read_text())
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb["ts"])).total_seconds()
        except Exception:
            pass
        running = self.running()
        health = "stopped"
        if running:
            if age is not None and age < 300:
                health = "healthy"
            else:
                try:
                    fresh = time.time() - self.pid_file.stat().st_mtime < 180
                except OSError:
                    fresh = False
                health = "starting" if fresh and (age is None or age >= 300) else "unresponsive"
        return {"running": running, "pid": self.pid() if running else None, "health": health,
                "heartbeat_age_s": round(age) if age is not None else None, "heartbeat": hb,
                "kill_file_present": self.kill_file.exists(),
                "stop_pending": self.stop_file.exists()}

    # ---- actions --------------------------------------------------------
    def start(self, send_orders: bool = False) -> dict:
        if self.running():
            return {"ok": False, "message": "already running"}
        if self.kill_file.exists():
            return {"ok": False, "message": "kill file present: clear it first (it flattens positions on start)"}
        self.stop_file.unlink(missing_ok=True)
        self.console_log.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self.python, str(self.script)] + (["--send-orders"] if send_orders else [])
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        with open(self.console_log, "ab") as log:
            proc = subprocess.Popen(cmd, cwd=self.root, stdout=log, stderr=log,
                                    stdin=subprocess.DEVNULL, **kwargs)
        # wait briefly: the bot exits at once on a failed MT5 connect / live-account refusal
        for _ in range(20):
            time.sleep(0.25)
            if proc.poll() is not None:
                return {"ok": False, "message": f"bot exited immediately (code {proc.returncode}); "
                                                f"see console log"}
            if self.pid() == proc.pid:
                break
        return {"ok": True, "message": f"started pid {proc.pid} "
                                       f"({'ORDERS ENABLED' if send_orders else 'dry-run'})"}

    def stop(self, timeout: float = 30.0) -> dict:
        """Graceful: bot leaves its loop, open positions stay open (stops remain at broker)."""
        if not self.running():
            return {"ok": True, "message": "not running"}
        pid = self.pid()
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text("stop")
        end = time.time() + timeout
        while time.time() < end:
            if not pid_alive(pid):
                self.stop_file.unlink(missing_ok=True)
                return {"ok": True, "message": "stopped gracefully (positions left open)"}
            time.sleep(0.5)
        _terminate(pid)
        self.stop_file.unlink(missing_ok=True)
        self.pid_file.unlink(missing_ok=True)
        return {"ok": True, "message": "did not stop in time: terminated"}

    def flatten_and_stop(self, timeout: float = 60.0) -> dict:
        """Emergency: close every position this bot owns, then exit."""
        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        self.kill_file.write_text(datetime.now(timezone.utc).isoformat())
        if not self.running():
            return {"ok": True, "message": "kill file set; bot is not running, so nothing was flattened. "
                                           "Start the bot to flatten, or close positions in MT5."}
        pid, end = self.pid(), time.time() + timeout
        while time.time() < end:
            if not pid_alive(pid):
                return {"ok": True, "message": "positions flattened, bot stopped. Kill file stays "
                                               "until you clear it."}
            time.sleep(0.5)
        return {"ok": False, "message": "bot still running after timeout: check the error log"}

    def clear_kill(self) -> dict:
        self.kill_file.unlink(missing_ok=True)
        return {"ok": True, "message": "kill file cleared"}

    def restart(self, send_orders: bool = False) -> dict:
        s = self.stop()
        if not s["ok"]:
            return s
        r = self.start(send_orders)
        r["message"] = s["message"] + "; " + r["message"]
        return r
