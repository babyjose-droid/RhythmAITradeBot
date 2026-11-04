"""Phase 9 core: schedule slots on the exchange calendar, metrics rendering."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.monitoring.metrics import Registry
from rhythm.scheduler import slots

CAL = SessionCalendar()
FRI = date(2026, 10, 30)
MON = date(2026, 11, 2)


def ist(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=EXCHANGE_TZ)


def test_intraday_slots_follow_completed_bars() -> None:
    first = slots.next_slot("intraday_5m", None, ist(FRI, 8), CAL)
    assert first == ist(FRI, 9, 20)
    day = slots.slots_between("intraday_5m", None, ist(FRI, 0), ist(FRI, 23), CAL)
    assert day[0] == ist(FRI, 9, 20) and day[-1] == ist(FRI, 15, 30) and len(day) == 75
    assert slots.next_slot("intraday_5m", None, ist(FRI, 15, 30), CAL) == ist(MON, 9, 20)  # weekend


def test_after_close_daily_at_every_minutes() -> None:
    assert slots.next_slot("after_close", None, ist(FRI, 15, 45), CAL) == ist(MON, 15, 45)
    assert slots.next_slot("daily_at", "18:30", ist(FRI, 19), CAL) == ist(MON, 18, 30)
    assert slots.next_slot("daily_at", "18:30", ist(FRI, 19), CAL, trading_days_only=False) == ist(
        date(2026, 10, 31), 18, 30
    )
    nxt = slots.next_slot("every_minutes", "15", ist(FRI, 10, 7), CAL)
    assert nxt == ist(FRI, 10, 15)
    sat = slots.next_slot(
        "every_minutes", "15", ist(date(2026, 10, 31), 10), CAL, trading_days_only=False
    )
    assert sat == ist(date(2026, 10, 31), 10, 15)


@pytest.mark.parametrize(
    ("schedule", "arg"),
    [
        ("daily_at", None),
        ("daily_at", "25:00"),
        ("every_minutes", "0"),
        ("intraday_5m", "x"),
        ("weekly", None),
    ],
)
def test_schedule_validation(schedule: str, arg: str | None) -> None:
    with pytest.raises(slots.ScheduleError):
        slots.validate(schedule, arg)


def test_slots_between_counts_missed_sessions() -> None:
    missed = slots.slots_between(
        "after_close", None, ist(FRI, 16), ist(FRI, 16) + timedelta(days=5), CAL
    )
    assert [m.date() for m in missed] == [date(2026, 11, d) for d in (2, 3, 4)]


def test_metrics_registry_text_format() -> None:
    r = Registry()
    r.observe_request("GET", "/v1/x", 200, 0.004)
    r.observe_request("GET", "/v1/x", 200, 0.3)
    r.observe_request("POST", '/v1/"y"', 403, 7.0)
    text = "\n".join(r.render_requests())
    assert 'rhythm_http_requests_total{method="GET",route="/v1/x",status="200"} 2' in text
    assert 'le="0.005",method="GET",route="/v1/x"} 1' in text
    assert 'le="+Inf",method="GET",route="/v1/x"} 2' in text
    assert 'route="/v1/\\"y\\""' in text  # label values are escaped
    assert "# TYPE rhythm_http_request_duration_seconds histogram" in text
