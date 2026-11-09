from pathlib import Path
import asyncio
from threading import Event

import httpx
from fastapi.testclient import TestClient

from scripted import planning
from agent_harness.web import create_app
from test_workbench import ReportPeer, create_task


def test_reconnect_resumes_after_saved_cursor_without_restarting_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    release = Event()
    peer = ReportPeer()

    async def held(request: httpx.Request) -> httpx.Response:
        if not peer.requests:
            assert await asyncio.to_thread(release.wait, 5)
        return await peer(request)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(held)))) as client:
        run = create_task(client, source)
        received = []
        try:
            with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
                while True:
                    event = stream.receive_json()
                    received.append(event)
                    if event["kind"] == "model.requested":
                        break
            cursor = received[-1]["sequence"]
            assert client.get(f"/api/runs/{run['id']}").json()["status"] == "running"
            with client.websocket_connect(f"/api/runs/{run['id']}/stream?after={cursor}") as stream:
                release.set()
                first = stream.receive_json()
                assert first["sequence"] == cursor + 1
                received.append(first)
                while received[-1]["kind"] not in {"run.completed", "run.failed"}:
                    received.append(stream.receive_json())
            saved = client.get(f"/api/runs/{run['id']}/events").json()["events"]
            assert received == saved
            final = client.get(f"/api/runs/{run['id']}").json()
            assert final["status"] == "completed"
            assert len(client.get(f"/api/sessions/{run['session_id']}/runs").json()) == 1
            assert len(final["artifacts"]) == 1
        finally:
            release.set()

import pytest
from starlette.websockets import WebSocketDisconnect
from test_workbench import finish


@pytest.mark.parametrize("cursor", ["-1", "999999999999999999", "oops", "1.5", ""])
def test_invalid_or_future_cursor_has_an_explicit_result(tmp_path: Path, cursor: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run, _ = finish(client, create_task(client, source))
        with client.websocket_connect(f"/api/runs/{run['id']}/stream?after={cursor}") as stream:
            with pytest.raises(WebSocketDisconnect) as closed:
                stream.receive_json()
            assert closed.value.code == 4400
            assert closed.value.reason == "invalid_cursor"

def test_repeated_submission_key_returns_original_run_after_completion_and_reopen(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(ReportPeer()))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        url = f"/api/sessions/{session['id']}/runs"
        body = {"task": "Save snapshot.", "sources": ["facts.md"], "submission_key": "submission-001"}
        response = client.post(url, json=body)
        assert response.status_code == 202, response.text
        original, events = finish(client, response.json())
        (source / "facts.md").unlink()
        repeat = client.post(url, json=body)
        assert repeat.status_code == 202, repeat.text
        assert repeat.json() == original
        assert len(client.get(url).json()) == 1
        assert client.get(f"/api/runs/{original['id']}/events").json()["events"] == events
    with TestClient(create_app(store)) as client:
        repeat = client.post(url, json=body)
        assert repeat.status_code == 202, repeat.text
        assert repeat.json() == original

def test_concurrent_retries_share_one_execution_and_changed_content_conflicts(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    release = Event()
    peer = ReportPeer()
    async def held(request: httpx.Request) -> httpx.Response:
        if not peer.requests:
            assert await asyncio.to_thread(release.wait, 10)
        return await peer(request)
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(held)))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        url = f"/api/sessions/{session['id']}/runs"
        body = {"task": "Save snapshot.", "sources": ["facts.md"], "submission_key": "concurrent-key"}
        try:
            with ThreadPoolExecutor(max_workers=8) as workers:
                responses = list(workers.map(lambda _: client.post(url, json=body), range(8)))
            assert [r.status_code for r in responses] == [202] * 8
            assert len({r.json()["id"] for r in responses}) == 1
            assert len(client.get(url).json()) == 1
            for change in [{"task": "Different task"}, {"require_publish_approval": True}, {"limits": {"max_steps": 8}}, {"sources": ["unselected.md"]}]:
                conflict = client.post(url, json={**body, **change})
                assert conflict.status_code == 409, conflict.text
                assert "different request content" in conflict.json()["detail"]
            assert client.post(url, json={**body, "submission_key": "another-key"}).status_code == 409
        finally:
            release.set()
        run, events = finish(client, responses[0].json())
        assert run["status"] == "completed" and len(run["artifacts"]) == 1
        assert sum(e["kind"] == "run.accepted" for e in events) == 1
        assert sum(e["kind"] == "run.started" for e in events) == 1
        assert len(peer.requests) == 4

def test_slow_ack_client_is_disconnected_without_blocking_persistence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run = create_task(client, source)
        with client.websocket_connect(f"/api/runs/{run['id']}/stream?flow=ack") as slow:
            final, saved = finish(client, run)
            assert final["status"] == "completed"
            delivered = []
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    delivered.append(slow.receive_json())
            assert closed.value.code == 1013
            assert closed.value.reason == "slow_client"
            assert 1 <= len(delivered) <= 8
            assert delivered == saved[:len(delivered)]
            cursor = delivered[-1]["sequence"]
        with client.websocket_connect(f"/api/runs/{run['id']}/stream?after={cursor}") as resumed:
            while delivered[-1]["kind"] != "run.completed":
                delivered.append(resumed.receive_json())
        assert delivered == saved

def test_acknowledged_replay_and_terminal_cursor_close_cleanly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run, saved = finish(client, create_task(client, source))
        received = []
        with client.websocket_connect(f"/api/runs/{run['id']}/stream?flow=ack&after=3") as stream:
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    event = stream.receive_json()
                    received.append(event)
                    stream.send_json({"ack": event["sequence"]})
            assert closed.value.code == 1000
        assert received == saved[3:]
        with client.websocket_connect(f"/api/runs/{run['id']}/stream?after={saved[-1]['sequence']}") as stream:
            with pytest.raises(WebSocketDisconnect) as closed:
                stream.receive_json()
            assert closed.value.code == 1000


@pytest.mark.parametrize("ack", [{"ack": -1}, {"ack": 999999}, {"ack": True}, {"other": 1}, {"ack": "1"}])
def test_invalid_ack_cannot_skip_events(tmp_path: Path, ack: dict) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run, _ = finish(client, create_task(client, source))
        with client.websocket_connect(f"/api/runs/{run['id']}/stream?flow=ack") as stream:
            stream.receive_json()
            stream.send_json(ack)
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    stream.receive_json()
            assert closed.value.code == 4400 and closed.value.reason == "invalid_ack"


def test_invalid_ack_close_during_replay_never_sends_after_close(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"x" * 200000)
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run, events = finish(client, create_task(client, source))
        after = next(event['sequence'] for event in events if event['kind']=='tool.completed' and event['data']['tool']=='source_read')-1
        for _ in range(40):
            with client.websocket_connect(f"/api/runs/{run['id']}/stream?flow=ack&after={after}") as stream:
                stream.receive_json()
                stream.send_json({"ack": 999999})
                with pytest.raises(WebSocketDisconnect) as closed:
                    while True:
                        stream.receive_json()
                assert closed.value.code == 4400 and closed.value.reason == "invalid_ack"
