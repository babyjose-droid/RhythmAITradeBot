"""When a job is due. Pure functions over the exchange calendar (IST)."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, ensure_utc

INTRADAY_STEP = timedelta(minutes=5)
FIRST_INTRADAY_SLOT = timedelta(minutes=5)  # the first 5-minute bar completes at 09:20
AFTER_CLOSE_DELAY = timedelta(minutes=15)  # daily bars are final ~15 minutes after the close
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_SCAN_DAYS = 30


class ScheduleError(ValueError):
    pass


def validate(schedule: str, arg: str | None) -> None:
    if schedule == "daily_at":
        if arg is None or not _HHMM.match(arg):
            raise ScheduleError("daily_at needs schedule_arg 'HH:MM' (IST)")
    elif schedule == "every_minutes":
        if arg is None or not arg.isdigit() or not 1 <= int(arg) <= 1440:
            raise ScheduleError("every_minutes needs schedule_arg 1-1440")
    elif schedule in ("intraday_5m", "after_close"):
        if arg:
            raise ScheduleError(f"{schedule} takes no schedule_arg")
    else:
        raise ScheduleError(f"unknown schedule {schedule!r}")


def _day_slots(
    schedule: str, arg: str | None, d: date, cal: SessionCalendar, trading_only: bool
) -> list[datetime]:
    trading = cal.is_trading_day(d)
    if schedule == "intraday_5m":
        if not trading:
            return []
        t, close = cal.session_open(d) + FIRST_INTRADAY_SLOT, cal.session_close(d)
        out = []
        while t <= close:
            out.append(t)
            t += INTRADAY_STEP
        return out
    if schedule == "after_close":
        return [cal.session_close(d) + AFTER_CLOSE_DELAY] if trading else []
    if trading_only and not trading:
        return []
    if schedule == "daily_at":
        hh, mm = (int(x) for x in (arg or "00:00").split(":"))
        return [datetime.combine(d, time(hh, mm), tzinfo=EXCHANGE_TZ)]
    step = timedelta(minutes=int(arg or "60"))
    start = datetime.combine(d, time(0, 0), tzinfo=EXCHANGE_TZ)
    n = int(timedelta(days=1) / step)
    return [start + step * i for i in range(n)]


def next_slot(
    schedule: str,
    arg: str | None,
    after: datetime,
    cal: SessionCalendar,
    *,
    trading_days_only: bool = True,
) -> datetime | None:
    """The first slot strictly after ``after`` (UTC-aware), or None within MAX_SCAN_DAYS."""
    validate(schedule, arg)
    after = ensure_utc(after)
    d = after.astimezone(EXCHANGE_TZ).date()
    for _ in range(MAX_SCAN_DAYS):
        for s in _day_slots(schedule, arg, d, cal, trading_days_only):
            if s > after:
                return s
        d += timedelta(days=1)
    return None


def slots_between(
    schedule: str,
    arg: str | None,
    start: datetime,
    end: datetime,
    cal: SessionCalendar,
    *,
    trading_days_only: bool = True,
) -> list[datetime]:
    """All slots in (start, end] — used to count what a stopped scheduler missed."""
    out: list[datetime] = []
    cur = start
    while True:
        s = next_slot(schedule, arg, cur, cal, trading_days_only=trading_days_only)
        if s is None or s > end:
            return out
        out.append(s)
        cur = s
