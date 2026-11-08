from pathlib import Path
import json
import asyncio
from threading import Event

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_harness_demo.web import create_app


def test_workspace_and_source_catalog_survive_reopening(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"

    with TestClient(create_app(store)) as client:
        response = client.post("/api/workspaces", json={"path": str(source)})
        assert response.status_code == 201, response.text
        workspace = response.json()
        assert workspace["name"] == "sources"

    with TestClient(create_app(store)) as client:
        saved = client.get("/api/workspaces").json()
        assert saved == [workspace]
        catalog = client.get(f"/api/workspaces/{workspace['id']}/sources").json()
        assert catalog["sources"] == [{"path": "facts.md", "bytes": 16}]


def tool_reply(name: str, arguments: dict, identity: str = "call") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": identity, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }}]})


class ReportPeer:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        results = [item for item in body["messages"] if item["role"] == "tool"]
        if not results:
            return tool_reply("workspace_list", {}, "catalog")
        if len(results) == 1:
            return tool_reply("source_read", {"path": "facts.md"}, "read")
        if len(results) == 2:
            content = "# Snapshot\n" + json.loads(results[-1]["content"])["text"]
            return tool_reply("artifact_write", {"name": "snapshot.md", "content": content}, "write")
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "Saved the source snapshot.",
        }}]})


def test_task_runs_real_tools_streams_events_and_preserves_download(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = ReportPeer()
    store = tmp_path / "store"

    with TestClient(create_app(store, model_transport=httpx.MockTransport(peer))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session_response = client.post("/api/sessions", json={
            "workspace_id": workspace["id"], "title": "Revenue brief",
        })
        assert session_response.status_code == 201, session_response.text
        session = session_response.json()
        response = client.post(f"/api/sessions/{session['id']}/runs", json={
            "task": "Read the selected source and save a snapshot.", "sources": ["facts.md"],
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        events = []
        with client.websocket_connect(f"/api/runs/{run_id}/stream") as stream:
            while True:
                event = stream.receive_json()
                events.append(event)
                if event["kind"] in {"run.completed", "run.failed"}:
                    break
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "completed", run
        assert run["mode"] == "fixture"
        assert run["usage"] == {"model_calls": 4, "tool_calls": 3}
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert [event["data"]["tool"] for event in events if event["kind"] == "tool.completed"] == [
            "workspace_list", "source_read", "artifact_write",
        ]
        saved_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
        assert saved_events == events
        artifact = run["artifacts"][0]
        expected = b"# Snapshot\nRevenue was 42.\n"
        assert client.get(artifact["download_url"]).content == expected
        assert json.loads(peer.requests[2]["messages"][-1]["content"])["text"] == "Revenue was 42.\n"

    with TestClient(create_app(store)) as client:
        assert client.get(f"/api/runs/{run_id}").json() == run
        assert client.get(artifact["download_url"]).content == expected


def create_task(client: TestClient, source: Path, **overrides: object) -> dict:
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    response = client.post(f"/api/sessions/{session['id']}/runs", json={
        "task": "Save a source snapshot.", "sources": ["facts.md"], **overrides,
    })
    assert response.status_code == 202, response.text
    return response.json()


def finish(client: TestClient, run: dict) -> tuple[dict, list[dict]]:
    events = []
    with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
        while True:
            event = stream.receive_json()
            events.append(event)
            if event["kind"] in {"run.completed", "run.failed", "run.interrupted", "run.cancelled"}:
                return client.get(f"/api/runs/{run['id']}").json(), events


def test_websocket_is_live_and_disconnecting_does_not_stop_the_run(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    release = Event()
    peer = ReportPeer()

    async def held(request: httpx.Request) -> httpx.Response:
        if not peer.requests:
            assert await asyncio.to_thread(release.wait, 5), "Test did not release the model transport."
        return await peer(request)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(held))) as client:
        run = create_task(client, source)
        try:
            with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
                while stream.receive_json()["kind"] != "model.requested":
                    pass
                saved = client.get(f"/api/runs/{run['id']}/events").json()["events"]
                assert saved[-1]["kind"] == "model.requested"
                assert client.get(f"/api/runs/{run['id']}").json()["status"] == "running"
        finally:
            release.set()
        completed, _ = finish(client, run)
        assert completed["status"] == "completed"
        assert client.get(completed["artifacts"][0]["download_url"]).status_code == 200


@pytest.mark.parametrize(("tool", "arguments", "code"), [
    ("artifact_write", {"name": "../escape.md", "content": "must not publish"}, "invalid_arguments"),
    ("source_read", {"path": "unselected.md"}, "source_forbidden"),
    ("shell", {"command": "anything"}, "unknown_tool"),
])
def test_invalid_or_ungranted_tool_is_rejected_before_dispatch(
    tmp_path: Path, tool: str, arguments: dict, code: str,
) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Selected facts.")
    (source / "unselected.md").write_bytes(b"Not authorized for this task.")

    async def peer(request: httpx.Request) -> httpx.Response:
        return tool_reply(tool, arguments)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        failed, events = finish(client, create_task(client, source, limits={"max_corrections": 0} if code == "invalid_arguments" else {}))
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == code
        assert failed["artifacts"] == []
        assert not any(event["kind"] == "tool.dispatched" for event in events)
        assert any(event["kind"] == "tool.rejected" for event in events)


def test_foreign_origin_cannot_register_a_local_directory(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        response = client.post("/api/workspaces", json={"path": str(tmp_path)}, headers={
            "Origin": "https://foreign.example",
        })
        assert response.status_code == 403
        assert client.get("/api/workspaces").json() == []


def test_workbench_entry_and_bundled_fixture_are_usable(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Agent Harness" in page.text
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/styles.css").status_code == 200
        config = client.get("/api/config").json()
        assert config["mode"] == "fixture"
        assert config["model"]["name"] == "fixture-script-v1"
        workspace = client.post("/api/workspaces", json={"path": config["demo_path"]}).json()
        sources = client.get(f"/api/workspaces/{workspace['id']}/sources").json()["sources"]
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={
            "task": "Save selected sources.", "sources": [item["path"] for item in sources],
        }).json()
        completed, _ = finish(client, run)
        assert completed["status"] == "completed", completed
        assert completed["mode"] == "fixture"
        artifact = completed["artifacts"][0]
        text = client.get(artifact["download_url"]).text
        assert "訂單追蹤" in text
        assert "模擬模型" in text


@pytest.mark.parametrize(("limits", "expected"), [
    ({"max_steps": 1}, "step_limit_exceeded"),
    ({"max_tool_calls": 1}, "tool_limit_exceeded"),
])
def test_limits_stop_further_model_or_tool_work(tmp_path: Path, limits: dict, expected: str) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run, events = finish(client, create_task(client, source, limits=limits))
        assert run["status"] == "failed"
        assert run["error"]["code"] == expected
        assert run["artifacts"] == []
        assert len([event for event in events if event["kind"] == "tool.dispatched"]) == 1
        assert events[-1]["kind"] == "run.failed"


def test_model_wait_is_stopped_at_the_run_deadline(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"A fact.")

    async def never_returns(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("The model wait should have been cancelled.")

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(never_returns))) as client:
        run, events = finish(client, create_task(client, source, limits={"timeout_seconds": 0.05}))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "deadline_exceeded"
        assert not any(event["kind"] == "tool.dispatched" for event in events)


def test_model_cannot_claim_success_without_a_published_artifact(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"A fact.")

    async def false_claim(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "Done! The report is ready at an invented download link.",
        }}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(false_claim))) as client:
        run, _ = finish(client, create_task(client, source))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "missing_artifact"
        assert run["artifacts"] == []


def test_changed_source_is_not_silently_read_as_the_admitted_version(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    fact = source / "facts.md"
    fact.write_bytes(b"Original fact.")

    async def changes_source(request: httpx.Request) -> httpx.Response:
        fact.write_bytes(b"Changed after admission.")
        return tool_reply("source_read", {"path": "facts.md"})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(changes_source))) as client:
        run, _ = finish(client, create_task(client, source))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "source_changed"
        assert run["artifacts"] == []


def test_modified_artifact_is_not_served_as_the_original(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(ReportPeer()))) as client:
        run, _ = finish(client, create_task(client, source))
        artifact = run["artifacts"][0]
        (store / "artifacts" / artifact["id"]).write_bytes(b"Externally changed.")
        assert client.get(artifact["download_url"]).status_code == 409


def test_source_preview_rejects_paths_outside_the_catalog(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Allowed preview.")
    (tmp_path / "outside.md").write_bytes(b"Outside.")
    with TestClient(create_app(tmp_path / "store")) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        assert client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": "facts.md"}).json()["text"] == "Allowed preview."
        assert client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": "../outside.md"}).status_code == 422


def test_largest_admitted_fixture_snapshot_remains_downloadable(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    names, contents = [], []
    for index in range(8):
        name = f"source-{index}.md"
        prefix = f"Source {index}: ".encode()
        content = prefix + b"x" * (262_144 - len(prefix))
        (source / name).write_bytes(content)
        names.append(name)
        contents.append(content.decode())
    with TestClient(create_app(tmp_path / "store")) as client:
        completed, _ = finish(client, create_task(client, source, sources=names))
        assert completed["status"] == "completed", completed["error"]
        artifact = completed["artifacts"][0]
        response = client.get(artifact["download_url"])
        assert response.status_code == 200
        for content in contents:
            assert content in response.text
