from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
from fastapi.testclient import TestClient

from scripted import planning
from agent_harness.web import create_app
from test_workbench import ReportPeer, create_task, finish
from test_analysis_task import AnalysisPeer


def pending_approval(client: TestClient, run: dict) -> dict:
    rejections: list[dict] = []
    with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
        while True:
            event = stream.receive_json()
            if event["kind"] == "approval.requested":
                return event["data"]
            if event["kind"] == "correction.available":
                rejections.append(event["data"]["error"])
            assert event["kind"] not in {"run.failed", "run.completed"}, (event["kind"], event["data"], rejections)


def test_exact_publish_approval_is_required_before_artifact_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run = create_task(client, source, require_publish_approval=True)
        with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
            while True:
                event = stream.receive_json()
                if event["kind"] == "approval.requested":
                    approval = event["data"]
                    break
                assert event["kind"] not in {"run.failed", "run.completed"}, event
        waiting = client.get(f"/api/runs/{run['id']}").json()
        assert waiting["status"] == "waiting_approval"
        assert waiting["artifacts"] == []
        assert approval["tool"] == "artifact_write" and approval["call_id"] == "write"
        assert approval["arguments"] == {"name": "snapshot.md", "content": "# Snapshot\nRevenue was 42.\n"}
        assert approval["scope"]["sources"] == waiting["sources"]
        assert len(approval["parameters_sha256"]) == 64
        events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)
        response = client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={
            "decision": "approve", "parameters_sha256": approval["parameters_sha256"],
        })
        assert response.status_code == 200, response.text
        final, events = finish(client, run)
        assert final["status"] == "completed", final["error"]
        assert final["approvals"][0]["status"] == "consumed"
        assert client.get(final["artifacts"][0]["download_url"]).content == b"# Snapshot\nRevenue was 42.\n"
        assert sum(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events) == 1
        assert client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 409


def test_waiting_approvals_do_not_occupy_all_execution_slots(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        waiting = []
        for _ in range(4):
            run = create_task(client, source, require_publish_approval=True)
            waiting.append((run, pending_approval(client, run)))
        fast = create_task(client, source)
        finished = Event()

        def finish_fast() -> dict:
            result, _ = finish(client, fast)
            finished.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(finish_fast)
            try:
                progressed = finished.wait(5)
                assert all(client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_approval" for run, _ in waiting)
            finally:
                for run, approval in waiting:
                    client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={"decision": "deny", "parameters_sha256": approval["parameters_sha256"]})
            assert result.result(timeout=5)["status"] == "completed"
            assert progressed, "A fifth Session could not finish while four other Sessions waited for user approval."


def test_wrong_parameters_and_denial_cannot_publish_or_release_another_call(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run = create_task(client, source, require_publish_approval=True)
        approval = pending_approval(client, run)
        url = f"/api/runs/{run['id']}/approvals/{approval['id']}"
        assert client.post(url, json={"decision": "approve", "parameters_sha256": "0" * 64}).status_code == 409
        assert client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_approval"
        assert client.post(f"/api/sessions/{run['session_id']}/runs", json={"task": "Another task", "sources": ["facts.md"]}).status_code == 409
        assert client.post(url, json={"decision": "deny", "parameters_sha256": approval["parameters_sha256"]}).status_code == 200
        final, events = finish(client, run)
        assert final["error"]["code"] == "approval_denied" and final["status"] == "failed"
        assert not final["artifacts"] and final["approvals"][0]["status"] == "denied"
        assert client.post(url, json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 409
        second = create_task(client, source, require_publish_approval=True)
        second_approval = pending_approval(client, second)
        assert client.post(f"/api/runs/{second['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 404
        assert client.post(f"/api/runs/{second['id']}/approvals/{second_approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 409
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)


def test_expired_approval_cannot_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run = create_task(client, source, require_publish_approval=True, limits={"approval_timeout_seconds": 0.05})
        approval = pending_approval(client, run)
        final, events = finish(client, run)
        assert final["status"] == "failed" and final["error"]["code"] == "approval_expired"
        assert final["approvals"][0]["status"] == "expired" and not final["artifacts"]
        assert client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 409
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)


def test_approval_reviews_the_exact_effective_content_including_source_notes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"month,orders,exceptions\nJan,120,12\nFeb,150,9\nMar,180,9\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(AnalysisPeer()))) as client:
        run = create_task(client, source, sources=["facts.csv"], require_publish_approval=True)
        approval = pending_approval(client, run)
        content = approval["arguments"]["content"]
        assert "Exception percentage: 6.666667%" in content
        assert "facts.csv:L2-L4" in content and content.count("## Verified tool evidence") == 1
        assert approval["scope"]["effect"] == "immutable_artifact"
        url = f"/api/runs/{run['id']}/approvals/{approval['id']}"
        assert client.post(url, json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"], "arguments": {"content": "replacement"}}).status_code == 422
        assert client.post(url, json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 200
        final, _ = finish(client, run)
        assert client.get(final["artifacts"][0]["download_url"]).content == content.encode("utf-8")


def test_publish_approval_never_bypasses_filename_guard(tmp_path: Path) -> None:
    from test_workbench import tool_reply

    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"A fact.")

    async def traversal(request: httpx.Request) -> httpx.Response:
        return tool_reply("artifact_write", {"name": "../outside.md", "content": "Forbidden"}, "bad")

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(traversal)))) as client:
        run, events = finish(client, create_task(client, source, require_publish_approval=True, limits={"max_corrections": 0}))
        assert run["error"]["code"] == "invalid_arguments" and run["status"] == "failed"
        assert run["approvals"] == [] and run["artifacts"] == []
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] != "plan_update" for event in events)
