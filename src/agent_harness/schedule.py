"""When a routine starts on its own: a few fixed kinds of schedule, in the local time of the machine.

A schedule is one of
- {"every": "hourly", "minute": 15}
- {"every": "daily", "at": "09:00"}
- {"every": "weekdays", "at": "09:00"}            (Monday to Friday)
- {"every": "weekly", "at": "09:00", "weekday": 0}  (0 is Monday)

There is no free-form cron: these cover the common cases and can be checked at a glance. Slots are wall-clock times
on this machine. The workbench starts a slot only while it is running, and does not catch up on slots it missed:
after a restart the next slot is the first one still in the future.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KINDS = ("hourly", "daily", "weekdays", "weekly")


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    every: Literal["hourly", "daily", "weekdays", "weekly"]
    minute: int | None = Field(default=None, ge=0, le=59)
    at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6)

    @model_validator(mode="after")
    def _fields_match_the_kind(self) -> "Schedule":
        wanted = {"hourly": {"minute"}, "daily": {"at"}, "weekdays": {"at"}, "weekly": {"at", "weekday"}}[self.every]
        given = {name for name in ("minute", "at", "weekday") if getattr(self, name) is not None}
        if given != wanted:
            raise ValueError(f"A {self.every} schedule takes exactly: {', '.join(sorted(wanted))}.")
        return self

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


def next_after(schedule: dict[str, Any], after: datetime) -> datetime:
    """The first slot strictly after `after`, as an aware datetime in UTC.

    The slot is found on the local wall clock (a naive datetime is local time to `astimezone`), so a daily 09:00
    stays 09:00 across a change of UTC offset.
    """
    local = after.astimezone().replace(tzinfo=None)
    every = schedule["every"]
    if every == "hourly":
        slot = local.replace(minute=schedule["minute"], second=0, microsecond=0)
        if slot <= local:
            slot += timedelta(hours=1)
        return slot.astimezone().astimezone(timezone.utc)
    hour, minute = (int(part) for part in schedule["at"].split(":"))
    for days in range(8):
        day = local + timedelta(days=days)
        slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot <= local:
            continue
        if every == "weekdays" and slot.weekday() >= 5:
            continue
        if every == "weekly" and slot.weekday() != schedule["weekday"]:
            continue
        return slot.astimezone().astimezone(timezone.utc)
    raise AssertionError("every schedule has a slot within eight days")


def stamp(moment: datetime) -> str:
    """The one format next-run times are stored and compared in: UTC, to the second."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
