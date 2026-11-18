"""Routine schedules: the next slot, checked on the local wall clock, and the fields each kind takes."""
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_harness.schedule import Schedule, next_after, stamp


def local(text: str) -> datetime:
    """A wall-clock time on this machine (2026-11-16 is a Monday)."""
    return datetime.fromisoformat(text).astimezone()


def wall(moment: datetime) -> str:
    return moment.astimezone().strftime("%a %Y-%m-%d %H:%M")


@pytest.mark.parametrize(("schedule", "after", "expected"), [
    ({"every": "hourly", "minute": 15}, "2026-11-16T09:10", "Mon 2026-11-16 09:15"),
    ({"every": "hourly", "minute": 15}, "2026-11-16T09:15", "Mon 2026-11-16 10:15"),  # strictly after
    ({"every": "hourly", "minute": 0}, "2026-11-16T23:30", "Tue 2026-11-17 00:00"),
    ({"every": "daily", "at": "09:00"}, "2026-11-16T08:59", "Mon 2026-11-16 09:00"),
    ({"every": "daily", "at": "09:00"}, "2026-11-16T09:00", "Tue 2026-11-17 09:00"),
    ({"every": "weekdays", "at": "09:00"}, "2026-11-20T10:00", "Mon 2026-11-23 09:00"),  # Friday after the slot
    ({"every": "weekdays", "at": "09:00"}, "2026-11-21T08:00", "Mon 2026-11-23 09:00"),  # Saturday
    ({"every": "weekly", "at": "18:30", "weekday": 4}, "2026-11-16T12:00", "Fri 2026-11-20 18:30"),
    ({"every": "weekly", "at": "18:30", "weekday": 0}, "2026-11-16T18:30", "Mon 2026-11-23 18:30"),
])
def test_the_next_slot_is_the_first_one_strictly_after(schedule: dict, after: str, expected: str) -> None:
    assert wall(next_after(schedule, local(after))) == expected


def test_a_missed_slot_is_not_caught_up() -> None:
    """After a week away, a daily routine runs once at the next slot, not seven times."""
    after_a_week = local("2026-11-23T12:00")
    assert wall(next_after({"every": "daily", "at": "09:00"}, after_a_week)) == "Tue 2026-11-24 09:00"


def test_slots_are_stored_in_one_comparable_utc_format() -> None:
    slot = next_after({"every": "daily", "at": "09:00"}, local("2026-11-16T08:00"))
    assert stamp(slot).endswith("+00:00") and len(stamp(slot)) == 25
    assert stamp(slot) < stamp(slot + timedelta(seconds=1))


@pytest.mark.parametrize("body", [
    {"every": "daily"},                                    # needs at
    {"every": "daily", "at": "9:00"},                      # HH:MM
    {"every": "daily", "at": "24:00"},
    {"every": "hourly", "minute": 60},
    {"every": "hourly", "at": "09:00"},                    # hourly takes a minute, not a time
    {"every": "weekly", "at": "09:00"},                    # needs a weekday
    {"every": "weekly", "at": "09:00", "weekday": 7},
    {"every": "monthly", "at": "09:00"},
    {"every": "daily", "at": "09:00", "cron": "* * * * *"},
])
def test_each_kind_takes_exactly_its_own_fields(body: dict) -> None:
    with pytest.raises(ValidationError):
        Schedule.model_validate(body)


def test_valid_schedules_round_trip() -> None:
    for body in ({"every": "hourly", "minute": 0}, {"every": "weekdays", "at": "07:45"},
                 {"every": "weekly", "at": "23:59", "weekday": 6}):
        assert Schedule.model_validate(body).as_dict() == body
