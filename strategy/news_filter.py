"""News filter / kill-switch helpers for trading."""
from datetime import datetime
from typing import Optional


class NewsFilter:
    def __init__(self, config=None):
        self.cfg = config or {}
        self.enabled = self.cfg.get("news_kill_switch", True)
        self.blackout_minutes_before = self.cfg.get("news_blackout_minutes_before", 30)
        self.blackout_minutes_after = self.cfg.get("news_blackout_minutes_after", 30)

    def is_blocked(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.utcnow()
        return False

    def block_if_high_impact(self, event_time: Optional[datetime] = None, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.utcnow()
        if event_time is None:
            return False
        before = event_time - timedelta(minutes=self.blackout_minutes_before)
        after = event_time + timedelta(minutes=self.blackout_minutes_after)
        return before <= now <= after

from datetime import timedelta

