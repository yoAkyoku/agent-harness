"""Routines: a saved task that a schedule or another system starts with one authenticated POST.

A routine stores a Run request (task, sources and options) for a workspace. `POST /api/routines/{id}/trigger`
starts a Run from it and answers 202 as soon as the Run is accepted, not when it has finished. Each
routine has its own Session, which gives it a run history and means two runs of one routine never
overlap. A paused routine refuses triggers, triggers are limited per hour, and every Run still admits
the sources' current versions and keeps the Run's ordinary limits, approvals and verifiers: a routine
is a way to start a Run, never a way around one.
"""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import web
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply


def _workspace(client: TestClient, tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    return client.post("/api/workspaces", json={"path": str(source)}).json()


def _routine(client: TestClient, workspace: dict, **run: object) -> httpx.Response:
    return client.post("/api/routines", json={"workspace_id": workspace["id"], "name": "Daily snapshot",
                                              "run": {"task": "Save a source snapshot.", "sources": ["facts.md"], **run}})


def test_a_trigger_starts_a_run_and_the_routine_keeps_its_history(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        workspace = _workspace(client, tmp_path)
        created = _routine(client, workspace)
        assert created.status_code == 201, created.text
        routine = created.json()
        assert routine["name"] == "Daily snapshot" and routine["paused"] is False and routine["runs"] == []
        assert routine["request"]["task"] == "Save a source snapshot." and "submission_key" not in routine["request"]

        accepted = client.post(f"/api/routines/{routine['id']}/trigger")
        assert accepted.status_code == 202, accepted.text  # accepted, not finished
        first, _ = finish(client, accepted.json())
        assert first["status"] == "completed" and first["session_id"] == routine["session_id"]

        second, _ = finish(client, client.post(f"/api/routines/{routine['id']}/trigger").json())
        detail = client.get(f"/api/routines/{routine['id']}").json()
        assert [run["id"] for run in detail["runs"]] == [second["id"], first["id"]]  # newest first
        assert detail["runs"][0]["status"] == "completed" and detail["runs"][0]["error"] is None
        listed = client.get("/api/routines").json()
        assert [item["id"] for item in listed] == [routine["id"]] and listed[0]["last_run"]["id"] == second["id"]
        assert client.get("/api/routines/missing").status_code == 404
        assert client.post("/api/routines/missing/trigger").status_code == 404


def test_a_paused_routine_refuses_triggers_until_resumed_and_a_deleted_one_is_gone(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        assert client.post(f"/api/routines/{routine['id']}/pause").json()["paused"] is True
        refused = client.post(f"/api/routines/{routine['id']}/trigger")
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "routine_paused"
        assert client.post(f"/api/routines/{routine['id']}/resume").json()["paused"] is False
        run, _ = finish(client, client.post(f"/api/routines/{routine['id']}/trigger").json())

        assert client.delete(f"/api/routines/{routine['id']}").status_code == 204
        assert client.post(f"/api/routines/{routine['id']}/trigger").status_code == 404
        assert client.get("/api/routines").json() == []
        assert client.get(f"/api/runs/{run['id']}").json()["status"] == "completed"  # past runs stay inspectable


def test_runs_of_one_routine_do_not_overlap(tmp_path: Path) -> None:
    replies = [tool_reply("plan_update", {"steps": ["Read", "Publish"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read")]
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(CompletionPeer(replies)),
                     hold_first_tool="source_read")
    with TestClient(app) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        running = client.post(f"/api/routines/{routine['id']}/trigger")
        assert running.status_code == 202
        overlapping = client.post(f"/api/routines/{routine['id']}/trigger")
        assert overlapping.status_code == 409 and "active Run" in overlapping.text
        client.post(f"/api/runs/{running.json()['id']}/cancel")


def test_triggers_are_limited_per_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "ROUTINE_RUNS_PER_HOUR", 1)
    with TestClient(create_app(tmp_path / "store")) as client:
        routine = _routine(client, _workspace(client, tmp_path)).json()
        finish(client, client.post(f"/api/routines/{routine['id']}/trigger").json())
        limited = client.post(f"/api/routines/{routine['id']}/trigger")
        assert limited.status_code == 429 and limited.json()["detail"]["code"] == "routine_rate_limited"


def test_a_routine_is_checked_when_it_is_saved(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        workspace = _workspace(client, tmp_path)
        missing = client.post("/api/routines", json={"workspace_id": workspace["id"], "name": "x",
                                                     "run": {"task": "t", "sources": ["nope.md"]}})
        assert missing.status_code == 422 and missing.json()["detail"]["code"] == "source_forbidden"
        based = _routine(client, workspace, base_artifact={"run_id": "a" * 32, "artifact_id": "b" * 32})
        assert based.status_code == 422 and "base artifact" in based.text
        keyed = _routine(client, workspace, submission_key="abcdefgh12345678")
        assert keyed.status_code == 422
        assert client.post("/api/routines", json={"workspace_id": "nope", "name": "x",
                                                  "run": {"task": "t", "sources": ["facts.md"]}}).status_code == 404
        # A source that disappears later fails the trigger, and no Run is created for it.
        routine = _routine(client, workspace).json()
        (tmp_path / "source" / "facts.md").unlink()
        gone = client.post(f"/api/routines/{routine['id']}/trigger")
        assert gone.status_code == 422 and client.get(f"/api/routines/{routine['id']}").json()["runs"] == []


def test_runs_waiting_for_a_decision_are_listed_across_sessions(tmp_path: Path) -> None:
    """One place to see what needs the user: every Run paused at an approval, whichever Session or routine it is in."""
    with TestClient(create_app(tmp_path / "store")) as client:
        workspace = _workspace(client, tmp_path)
        assert client.get("/api/attention").json() == []
        routine = _routine(client, workspace, require_publish_approval=True).json()
        run = client.post(f"/api/routines/{routine['id']}/trigger").json()
        with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
            while stream.receive_json()["kind"] != "approval.requested":
                pass
        waiting = client.get("/api/attention").json()
        assert len(waiting) == 1
        item = waiting[0]
        assert item["run_id"] == run["id"] and item["session_id"] == routine["session_id"]
        assert item["session_title"] == "Routine: Daily snapshot" and item["workspace_id"] == workspace["id"]
        assert item["tool"] == "artifact_write" and item["expires_at"]
        approval = client.get(f"/api/runs/{run['id']}").json()["approvals"][0]
        client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}",
                    json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]})
        finish(client, run)
        assert client.get("/api/attention").json() == []
