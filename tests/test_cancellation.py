from pathlib import Path
import asyncio
import json
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient

from scripted import planning
from agent_harness_demo.web import create_app
from test_approval import pending_approval
from test_workbench import ReportPeer, create_task, finish, tool_reply


def test_cancelling_waiting_approval_invalidates_it_without_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run = create_task(client, source, require_publish_approval=True)
        approval = pending_approval(client, run)
        response = client.post(f"/api/runs/{run['id']}/cancel")
        assert response.status_code == 202, response.text
        final, events = finish(client, run)
        assert final["status"] == "cancelled"
        assert final["cancellation"]["outcome"] == "stopped"
        assert final["approvals"][0]["status"] == "cancelled" and not final["artifacts"]
        kinds = [event["kind"] for event in events]
        assert kinds.index("run.cancel_requested") < kinds.index("run.cancelled")
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)
        assert client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 409
        assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 202
        assert client.get(f"/api/runs/{run['id']}/events").json()["events"] == events


def test_cancellation_stops_runner_before_an_ignored_model_cancel_returns(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    started, release, returned, stopped = Event(), Event(), Event(), Event()

    async def late(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.to_thread(release.wait, 10)
        except asyncio.CancelledError:
            await asyncio.to_thread(release.wait, 10)
        returned.set()
        return tool_reply("artifact_write", {"name": "late.md", "content": "Late model output"}, "late")

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(late))) as client:
        run = create_task(client, source)
        assert started.wait(5)

        def wait_stopped() -> tuple[dict, list]:
            result = finish(client, run)
            stopped.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(wait_stopped)
            try:
                assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 202
                stopped_before_return = stopped.wait(2)
            finally:
                release.set()
            final, events = result.result(timeout=5)
        assert returned.wait(5)
        assert stopped_before_return, "The runner waited for an uncooperative provider instead of stopping its own work."
        assert final["status"] == "cancelled" and not final["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" for event in events)
        assert client.get(f"/api/runs/{run['id']}").json()["status"] == "cancelled"
        assert not client.get(f"/api/runs/{run['id']}").json()["artifacts"]


def test_another_session_finishes_while_a_dispatched_read_is_held(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()), hold_first_tool="source_read")) as client:
        slow = create_task(client, source)
        with client.websocket_connect(f"/api/runs/{slow['id']}/stream") as stream:
            while True:
                event = stream.receive_json()
                if event["kind"] == "validation.tool_held":
                    held = event["data"]
                    break
                assert event["kind"] not in {"run.failed", "run.completed"}, event
        assert held["tool"] == "source_read" and held["phase"] == "before_read"
        fast = create_task(client, source)
        fast_done = Event()

        def complete_fast() -> dict:
            final, _ = finish(client, fast)
            fast_done.set()
            return final

        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(complete_fast)
            try:
                assert fast_done.wait(5), "Session B did not complete while tool A remained held."
                assert client.get(f"/api/runs/{slow['id']}").json()["status"] == "running"
                assert client.post(f"/api/runs/{slow['id']}/cancel").status_code == 202
                cancelled, events = finish(client, slow)
            finally:
                client.post(f"/api/runs/{slow['id']}/validation/{held['call_id']}/release")
            assert result.result(timeout=5)["status"] == "completed"
        assert cancelled["status"] == "cancelled" and cancelled["cancellation"]["outcome"] == "stopped"
        assert not any(event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read" for event in events)
        assert not cancelled["artifacts"]


def test_cancel_after_publish_before_receipt_preserves_artifact_and_uncertainty(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = ReportPeer()
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(peer)), hold_first_tool="artifact_write")) as client:
        run = create_task(client, source)
        with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
            while True:
                event = stream.receive_json()
                if event["kind"] == "validation.tool_held":
                    held = event["data"]
                    break
                assert event["kind"] not in {"run.failed", "run.completed"}, event
        assert held["phase"] == "after_publish_before_result"
        before = client.get(f"/api/runs/{run['id']}").json()
        assert len(before["artifacts"]) == 1
        assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 202
        final, events = finish(client, run)
        assert final["status"] == "cancelled"
        assert final["cancellation"]["outcome"] == "unknown_effects"
        assert final["cancellation"]["uncertain_attempts"] == [{"call_id": "write", "tool": "artifact_write"}]
        assert final["cancellation"]["completed_artifacts"] == 1
        assert final["artifacts"] == before["artifacts"]
        assert client.get(final["artifacts"][0]["download_url"]).content == b"# Snapshot\nRevenue was 42.\n"
        assert len(peer.requests) == 3
        assert sum(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events) == 1
        assert client.post(f"/api/runs/{run['id']}/validation/write/release").status_code == 404


def test_cancel_after_approval_before_slot_dispatch_revokes_that_approval(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    four_started, release = Event(), Event()
    active, maximum = 0, 0
    peer = ReportPeer()

    async def capacity(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        body = json.loads(request.content)
        if body["messages"][1]["content"] == "Hold capacity":
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                four_started.set()
            try:
                assert await asyncio.to_thread(release.wait, 10), "Capacity test did not release model work."
            finally:
                active -= 1
        return await peer(request)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(capacity)))) as client:
        waiting = create_task(client, source, require_publish_approval=True)
        approval = pending_approval(client, waiting)
        blockers = [create_task(client, source, task="Hold capacity") for _ in range(4)]
        try:
            assert four_started.wait(5)
            response = client.post(f"/api/runs/{waiting['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]})
            assert response.status_code == 200
            assert client.get(f"/api/runs/{waiting['id']}").json()["approvals"][0]["status"] == "approved"
            assert client.post(f"/api/runs/{waiting['id']}/cancel").status_code == 202
            final, events = finish(client, waiting)
            assert final["status"] == "cancelled" and final["approvals"][0]["status"] == "cancelled"
            assert not final["artifacts"] and final["cancellation"]["uncertain_attempts"] == []
        finally:
            release.set()
        assert all(finish(client, blocker)[0]["status"] == "completed" for blocker in blockers)
        assert maximum == 4
        assert client.get(f"/api/runs/{waiting['id']}/events").json()["events"] == events
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)
