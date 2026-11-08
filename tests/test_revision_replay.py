"""A controlled replay of recorded public demo inputs; not a model-quality test."""
from pathlib import Path
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_local_model import LocalPeer
from test_session_versions import qwen_metadata
from test_workbench import finish, tool_reply


@pytest.mark.parametrize("keep_final_newline", [True, False])
def test_recorded_revision_fits_default_context_without_losing_plan_or_base(tmp_path: Path, keep_final_newline: bool) -> None:
    docs = Path(__file__).resolve().parents[1] / "tests/fixtures/recorded"
    original = json.loads((docs / "slice-04/real-9b-proposal-02/run.json").read_text(encoding="utf-8"))
    original_events = json.loads((docs / "slice-04/real-9b-proposal-02/events.json").read_text(encoding="utf-8"))
    if isinstance(original_events, dict):
        original_events = original_events["events"]
    seed_calls = [event["data"] for event in original_events if event["kind"] == "tool.requested"]
    revision = json.loads((docs / "slice-05/real-9b-02/run.json").read_text(encoding="utf-8"))
    revision_events = json.loads((docs / "slice-05/real-9b-02/events.json").read_text(encoding="utf-8"))
    revision_calls = [event["data"] for event in revision_events if event["kind"] == "tool.requested"][:2]
    source = tmp_path / "source"
    source.mkdir()
    (source / "brief.md").write_bytes((docs / "slice-04/real-9b-proposal-02/source-brief.md").read_bytes())
    seed_requests = 0
    requests: list[dict] = []
    proposed_contents: list[str] = []
    meta = LocalPeer()
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal seed_requests
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        if request.url.path == "/api/tags":
            return await meta(request)
        body = json.loads(request.content)
        last_user = next(message["content"] for message in reversed(body["messages"]) if message["role"] == "user")
        if last_user != revision["task"]:
            index = seed_requests
            seed_requests += 1
            if index < len(seed_calls):
                call = seed_calls[index]
                return tool_reply(call["tool"], call["arguments"], call["call_id"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Seed complete."}}]})
        requests.append(body)
        if len(requests) <= 2:
            call = revision_calls[len(requests)-1]
            return tool_reply(call["tool"], call["arguments"], call["call_id"])
        if len(requests) == 3:
            arguments = dict(seed_calls[-1]["arguments"])
            arguments["name"] = "brief-summary-v2.md"
            content = old_bytes.decode("utf-8").replace("# brief-summary.md", "# 客服訂單追蹤摘要", 1)
            arguments["content"] = content if keep_final_newline else content.rstrip("\r\n")
            proposed_contents.append(arguments["content"])
            return tool_reply("artifact_write", arguments, "revision-write")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Revision complete."}}]})

    store = tmp_path / "store"
    # Seed the exact old bytes through real tools, without pretending its
    # original generation has passed the newly introduced model envelope.
    with TestClient(create_app(store, model_transport=httpx.MockTransport(peer))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": original["task"], "sources": ["brief.md"]}).json()
        base_run, _ = finish(client, run)
        assert base_run["status"] == "completed", base_run["error"]
        base = base_run["artifacts"][0]
        assert base["sha256"] == "a7f11798a5105cb2118e305d5ea69dbd825773fc5ad9c6d8893095121ee40bbc"
        old_bytes = client.get(base["download_url"]).content
    with TestClient(create_app(store, model_settings=ModelSettings.local("contract-model", stream=False),
                               model_transport=httpx.MockTransport(peer))) as client:
        response = client.post(f"/api/sessions/{session['id']}/runs", json={
            "task": revision["task"], "sources": ["brief.md"],
            "base_artifact": {"run_id": base_run["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())
        assert final["status"] == "completed", (final["error"], [event["data"] for event in events if event["kind"] == "context.limited"])
        assert final["limits"]["context_window_tokens"] is None
        assert all(event["data"]["context_budget"]["capacity_tokens"] == 8192 for event in events if event["kind"] == "model.requested")
        assert len(requests) == 4
        assert all(any(message.get("content") == revision["task"] for message in body["messages"]) for body in requests)
        base_message = next(message["content"] for message in requests[0]["messages"]
                            if (message.get("content") or "").startswith("Pinned base artifact"))
        pinned = json.loads(base_message.split("\n",1)[1])
        assert "text" not in pinned and pinned["sha256"] == base["sha256"]
        assert client.get(f"/api/runs/{final['id']}").json()["base_artifact"]["text"].encode("utf-8") == old_bytes
        for body in requests[2:]:
            state = next((message["content"] for message in body["messages"]
                          if (message.get("content") or "").startswith("Saved executor state")), None)
            if state is not None:
                steps = json.loads(state.split("\n",1)[1])["plan_steps"]
            else:
                result = next(message["content"] for message in body["messages"]
                              if message.get("tool_call_id") == revision_calls[0]["call_id"])
                steps = json.loads(result)["steps"]
            assert steps == revision_calls[0]["arguments"]["steps"]
        assert client.get(base["download_url"]).content == old_bytes
        artifact = final["artifacts"][0]
        assert artifact["version"] == 2 and artifact["parent_artifact_id"] == base["id"]
        saved = client.get(artifact["download_url"]).text
        assert saved.startswith("# 客服訂單追蹤摘要\n")
        assert saved.count("## Verified tool evidence") == 1
        assert saved == proposed_contents[0]
