import time
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple


def parse_active_hours(value: str) -> Optional[Tuple[int, int]]:
    """'08:00-22:00' -> (480, 1320) in minutes after midnight; 'off' -> None. a window may wrap past midnight"""
    if value.strip().lower() in ('off', 'none', ''):
        return None
    try:
        start, end = (part.strip() for part in value.split('-'))
        minutes = []
        for part in (start, end):
            hours, mins = part.split(':')
            if not (0 <= int(hours) <= 24 and 0 <= int(mins) < 60):
                raise ValueError
            minutes.append(int(hours) * 60 + int(mins))
    except ValueError:
        raise ValueError(f"{value!r} is not a window like 08:00-22:00")
    if minutes[0] == minutes[1]:
        raise ValueError(f"{value!r} is an empty window")
    return minutes[0], minutes[1]


class CrawlBudget:
    """a daily request allowance and a window of active hours, both optional. before each request, wait()
    sleeps until one is allowed; spend() counts it. counts are kept through get_meta/set_meta, so every run
    against the same database shares one allowance. days and hours are local time (or tz)"""

    def __init__(
        self,
        get_meta: Callable[[str], Optional[str]],
        set_meta: Callable[[str, str], None],
        daily: Optional[int] = None,
        active_hours: Optional[Tuple[int, int]] = None,
        clock=None,
        tz=None
    ):
        self.get_meta = get_meta
        self.set_meta = set_meta
        self.daily = daily
        self.active_hours = active_hours
        self.clock = clock if clock is not None else time
        self.tz = tz

    def describe(self) -> str:
        parts = []
        if self.daily:
            parts.append(f"daily budget {self.daily:,} requests ({self._used(self._now()):,} used today)")
        if self.active_hours:
            start, end = self.active_hours
            parts.append(f"active {start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}")
        return ', '.join(parts) or 'no daily budget or active hours'

    def wait(self):
        while True:
            now = self._now()
            if self.active_hours and not self._inside(now):
                self._pause_until(self._next_start(now), 'outside the active hours')
                continue
            if self.daily and self._used(now) >= self.daily:
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                self._pause_until(tomorrow, f"today's budget of {self.daily:,} requests is used")
                continue
            return

    def spend(self):
        if not self.daily:
            return
        today = self._now().date().isoformat()
        used = self._used(self._now()) + 1
        self.set_meta('budget_day', today)
        self.set_meta('budget_used', str(used))

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.time(), tz=self.tz)

    def _used(self, now: datetime) -> int:
        if self.get_meta('budget_day') != now.date().isoformat():
            return 0
        return int(self.get_meta('budget_used') or 0)

    def _inside(self, now: datetime) -> bool:
        start, end = self.active_hours
        minute = now.hour * 60 + now.minute
        return start <= minute < end if start < end else minute >= start or minute < end

    def _next_start(self, now: datetime) -> datetime:
        start = self.active_hours[0]
        at = now.replace(hour=start // 60 % 24, minute=start % 60, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)

    def _pause_until(self, when: datetime, reason: str):
        seconds = max(1.0, (when - self._now()).total_seconds())
        print(f"    {reason}: pausing until {when:%Y-%m-%d %H:%M} ({seconds / 3600:.1f}h)")
        self.clock.sleep(seconds)
