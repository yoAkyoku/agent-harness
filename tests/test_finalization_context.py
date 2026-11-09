"""Publication finalization replaces the conversation; nothing may keep pointing at the dropped messages."""
import asyncio
import json
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer


def json_reply(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"calls": [{"name": name, "arguments": arguments}]})}}]})


@pytest.mark.parametrize("edit_memory", [False, True], ids=["memory_unchanged", "memory_edited"])
def test_memory_edit_while_publishing_does_not_break_finalization(tmp_path: Path, edit_memory: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps": ["Read and publish"]}),
                           json_reply("source_read", {"path": "facts.md", "labels": []}),
                           json_reply("artifact_write", candidate),
                           json_reply("task_finish", {"summary": "Saved snapshot.md with revenue 42."})])
    at_write, edited = Event(), Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        # Hold the publishing decision so the memory changes between publication and finalization.
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 2:
            at_write.set()
            assert await asyncio.to_thread(edited.wait, 10)
        return await peer(request)

    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(transport))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        memory = client.post(f"/api/workspaces/{workspace['id']}/memories",
                             json={"title": "Style", "content": "Be brief."}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs",
                          json={"task": "Save a source snapshot.", "sources": ["facts.md"]}).json()
        assert at_write.wait(10)
        if edit_memory:
            response = client.post(f"/api/workspaces/{workspace['id']}/memories/{memory['id']}/versions",
                                   json={"title": "Style", "content": "Be very brief.", "expected_version": 1})
            assert response.status_code == 201, response.text
        edited.set()
        with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
            while stream.receive_json()["kind"] not in {"run.completed", "run.failed"}:
                pass
        final = client.get(f"/api/runs/{run['id']}").json()

    assert final["status"] == "completed", final["error"]
    assert len(final["artifacts"]) == 1
    # The finalization request is the system message, the task and the published body only.
    finalization = peer.requests[-1]["messages"]
    assert [message["role"] for message in finalization] == ["system", "user", "user"]
    assert not any("workspace memory" in (message.get("content") or "") for message in finalization)
