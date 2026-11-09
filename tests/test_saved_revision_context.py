"""Diagnostic public replay of the captured 4B revision; no real model call."""
import json
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_local_model import LocalPeer, qwen_thinking_metadata
from test_workbench import create_task, finish
from test_approval import pending_approval

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/saved-revision-context.json"

def test_saved_revision_should_finish_after_approval(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("handoff.md", "release.md"):
        (source / name).write_bytes((ROOT / "evaluation/slice-09/documents-standard" / name).read_bytes())
    fixture = json.loads(FIXTURE.read_text(encoding="utf8"))
    replies = fixture["base_messages"] + fixture["revision_messages"]
    replies.append({"role":"assistant", "content":"已保存 index.md。"})
    peer = LocalPeer()
    requests = []
    async def transport(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version":"0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        if request.url.path != "/v1/chat/completions":
            return await peer(request)
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices":[{"finish_reason":"stop", "message":replies.pop(0)}]})
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(transport))) as client:
        base = create_task(client, source, sources=["handoff.md", "release.md"], task=fixture["base_task"], limits=fixture["base_limits"], require_publish_approval=True)
        approval = pending_approval(client, base)
        client.post(f"/api/runs/{base['id']}/approvals/{approval['id']}", json={"decision":"approve", "parameters_sha256":approval["parameters_sha256"]}).raise_for_status()
        base, _ = finish(client, base)
        assert base["status"] == "completed", base["error"]
        artifact = base["artifacts"][0]
        response = client.post(f"/api/sessions/{base['session_id']}/runs", json={"task":fixture["revision_task"], "sources":["handoff.md","release.md"], "limits":fixture["revision_limits"], "require_publish_approval":True, "base_artifact":{"run_id":base["id"],"artifact_id":artifact["id"]}})
        assert response.status_code == 202, response.text
        run = response.json()
        approval = pending_approval(client, run)
        client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={"decision":"approve", "parameters_sha256":approval["parameters_sha256"]}).raise_for_status()
        final, events = finish(client, run)
        assert final["artifacts"] and final["artifacts"][0]["version"] == 2
        assert final["status"] == "completed", {"error":final["error"], "limits":[e["data"] for e in events if e["kind"] == "context.limited"]}

        reads = [event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read"]
        assert len(reads) == 2 and all(read["labelled_lines"] and read["text"] for read in reads)
        published = next(event for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_write")
        projected = next((event for event in events if event["kind"] == "context.pruned"
                          and event["data"].get("reason") == "labelled_lines_projection"), None)
        if projected is not None:
            # Projection is a budget measure: only after publication, and only
            # dropping the derived labelled_lines while text and hash stay.
            assert projected["sequence"] > published["sequence"]
            assert set(projected["data"]["evidence_ids"]) == {read["evidence_id"] for read in reads}
            final_messages = requests[-1]["messages"]
            state = next(json.loads(message["content"].split("\n", 1)[1]) for message in final_messages if (message.get("content") or "").startswith("Saved executor state"))
            for read in reads:
                viewed = state["verified_evidence"][read["evidence_id"]]
                assert viewed["text"] == read["text"] and viewed["sha256"] == read["sha256"]
                assert "labelled_lines" not in viewed
        assert all(e["data"]["context_budget"]["upper_bound_tokens"] <= 8192 for e in events if e["kind"] == "model.requested")
        assert [e["data"] for e in events if e["kind"] == "model.requested"][-1]["summary_guidance_applied"] is False
        assert client.get(base["artifacts"][0]["download_url"]).content == fixture["base_text"].encode()
