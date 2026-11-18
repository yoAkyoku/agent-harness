"""Routine schedules: the workbench starts a routine on its own at fixed wall-clock times.

A scheduled start is an ordinary trigger (paused routines, the hourly limit, current source versions and the
Run's approvals all apply). Slots run only while the workbench is running and missed slots are not caught up:
one start at most per tick, then the next slot after now. A trigger that is refused leaves its error on the
routine instead of disappearing.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from fastapi.testclient import TestClient

from agent_harness import web
from agent_harness.web import create_app
from test_routines import _routine, _workspace
from test_workbench import finish

DAILY = {"every": "daily", "at": "09:00"}


def _tick(client: TestClient, moment: datetime) -> list[dict]:
    return client.portal.call(client.app.state.run_due_routines, moment)  # type: ignore[union-attr, attr-defined]


def _at(routine: dict) -> datetime:
    return datetime.fromisoformat(routine["next_run_at"])


def test_a_schedule_is_saved_with_its_next_slot_and_can_be_cleared(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        assert routine["schedule"] is None and routine["next_run_at"] is None and routine["schedule_error"] is None

        saved = client.put(f"/api/routines/{routine['id']}/schedule", json=DAILY)
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["schedule"] == DAILY
        slot = _at(body)
        assert slot > datetime.now(timezone.utc) and slot.astimezone().strftime("%H:%M") == "09:00"
        assert client.get("/api/routines").json()[0]["next_run_at"] == body["next_run_at"]

        refused = client.put(f"/api/routines/{routine['id']}/schedule", json={"every": "daily", "at": "25:00"})
        assert refused.status_code == 422
        assert client.put("/api/routines/missing/schedule", json=DAILY).status_code == 404

        cleared = client.delete(f"/api/routines/{routine['id']}/schedule")
        assert cleared.status_code == 200 and cleared.json()["schedule"] is None and cleared.json()["next_run_at"] is None


def test_a_due_slot_starts_one_run_and_moves_to_the_next_slot_without_catching_up(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        slot = _at(client.put(f"/api/routines/{routine['id']}/schedule", json=DAILY).json())

        assert _tick(client, slot - timedelta(seconds=1)) == []  # not yet
        started = _tick(client, slot + timedelta(seconds=5))
        assert len(started) == 1
        final, _ = finish(client, started[0])
        assert final["status"] == "completed" and final["session_id"] == routine["session_id"]
        after = client.get(f"/api/routines/{routine['id']}").json()
        assert _at(after) == slot + timedelta(days=1) and after["schedule_error"] is None
        assert _tick(client, slot + timedelta(seconds=5)) == []  # the same slot never starts twice

        three_days_late = slot + timedelta(days=3, hours=2)
        assert len(_tick(client, three_days_late)) == 1  # one start, not three
        assert _at(client.get(f"/api/routines/{routine['id']}").json()) == slot + timedelta(days=4)


def test_a_paused_routine_skips_its_slots_without_an_error(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        slot = _at(client.put(f"/api/routines/{routine['id']}/schedule", json=DAILY).json())
        client.post(f"/api/routines/{routine['id']}/pause")

        assert _tick(client, slot + timedelta(seconds=1)) == []
        skipped = client.get(f"/api/routines/{routine['id']}").json()
        assert skipped["runs"] == [] and skipped["schedule_error"] is None and _at(skipped) == slot + timedelta(days=1)


def test_a_refused_scheduled_start_keeps_its_error_until_a_start_succeeds(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        slot = _at(client.put(f"/api/routines/{routine['id']}/schedule", json=DAILY).json())
        source = tmp_path / "source" / "facts.md"
        source.rename(source.with_name("moved.md"))

        assert _tick(client, slot + timedelta(seconds=1)) == []
        failed = client.get(f"/api/routines/{routine['id']}").json()
        assert failed["runs"] == [] and failed["schedule_error"]["code"]
        assert failed["schedule_error"]["at"] == failed["schedule_error"]["at"][:25]  # when it happened, UTC
        assert _at(failed) == slot + timedelta(days=1)  # the schedule carries on

        source.with_name("moved.md").rename(source)
        started = _tick(client, slot + timedelta(days=1, seconds=1))
        assert len(started) == 1
        finish(client, started[0])
        assert client.get(f"/api/routines/{routine['id']}").json()["schedule_error"] is None


def test_the_workbench_runs_due_slots_on_its_own_while_it_is_running(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(web, "SCHEDULE_TICK_SECONDS", 0.05)
    monkeypatch.setattr(web, "next_after", lambda schedule, after: after + timedelta(seconds=0.3))
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        client.put(f"/api/routines/{routine['id']}/schedule", json=DAILY)
        deadline = time.monotonic() + 15
        while not client.get(f"/api/routines/{routine['id']}").json()["runs"]:
            assert time.monotonic() < deadline, "no scheduled Run started"
            time.sleep(.05)
        client.delete(f"/api/routines/{routine['id']}/schedule")
        run = client.get(f"/api/routines/{routine['id']}").json()["runs"][-1]
        final, _ = finish(client, client.get(f"/api/runs/{run['id']}").json())
        assert final["status"] == "completed"
