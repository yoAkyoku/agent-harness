"""Recorded real decisions replayed at public seams, without model-quality claims."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from recorded_citations import bracket_bare_eids, bracket_draft_calls, publish_current_draft
from test_approval import pending_approval
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_workbench import create_task, finish


def recorded_replies(path: Path) -> list[httpx.Response]:
    events = json.loads(path.read_bytes())["events"]
    return [httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": bracket_draft_calls(event["data"]["message"]["wire_content"])}}]})
        for event in events if event["kind"] == "model.responded"]


@pytest.mark.parametrize("include_error", [True, False])
def test_recorded_revision_can_review_publish_and_finish_without_losing_base_or_draft(tmp_path: Path, include_error: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    original = root / "tests/fixtures/recorded/gui-real-9b-01/after-approval/ba2622198f7d438d8888b871579b169e"
    revision = root / "tests/fixtures/recorded/gui-real-9b-revision-03"
    manifest = json.loads((revision / "manifest.json").read_bytes())
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-standard/metrics.csv").read_bytes())
    peer = CompletionPeer(recorded_replies(original / "events.json"))

    async def transport(request: httpx.Request) -> httpx.Response:
        response = await peer(request)
        if request.url.path == "/v1/chat/completions":
            response = publish_current_draft(response, json.loads(request.content))
            decision = json.loads(response.json()["choices"][0]["message"]["content"])
            if "reply" in decision:
                ack = json.loads(peer.requests[-1]["messages"][-1]["content"])["tool_result"]["value"]
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps({"reply": {"artifact_ids": [ack["id"]]}})}}]})
        return response

    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools"),
                               model_transport=httpx.MockTransport(transport))) as client:
        seed = create_task(client, source, sources=["metrics.csv"], publication_workflow="draft_review", completion_workflow="artifact_receipt", require_publish_approval=True,
                           task=json.loads((original / "run.json").read_bytes())["task"])
        approval = pending_approval(client, seed)
        client.post(f"/api/runs/{seed['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).raise_for_status()
        seeded, _ = finish(client, seed)
        assert seeded["status"] == "completed", seeded["error"]
        base = seeded["artifacts"][0]
        original_bytes = client.get(base["download_url"]).content
        recorded_original = (original / "artifacts/e9a40ac96973a6df02f66d94dc0fe3ec/report.md").read_text(encoding="utf-8")
        assert original_bytes == bracket_bare_eids(recorded_original).encode()
        peer.requests.clear()
        peer.replies = [
            *recorded_replies(revision / "failed/419185edfbf4447db63cc8d20872637b/events.json"),
            json_reply("artifact_write", {"name": "report.md", "draft_sha256": "8f145c09efd54fceef0e82c3727e8bca8f29785c3ef55d86d5155e4f87027780", "citations": ["E1", "E2", "E3", "E4"]}),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":{"artifact_ids":[]}}'}}]}),
        ]
        if not include_error:
            del peer.replies[3]
        response = client.post(f"/api/sessions/{seeded['session_id']}/runs", json={
            "task": manifest["task"], "sources": ["metrics.csv"], "limits": manifest["limits"],
            "publication_workflow": "draft_review", "completion_workflow": "artifact_receipt", "require_publish_approval": True,
            "base_artifact": {"run_id": seeded["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        run = response.json()
        approval = pending_approval(client, run)
        expected_revision = bracket_bare_eids((revision / "unpublished-draft.md").read_text(encoding="utf-8")).encode()
        assert approval["arguments"]["content"].encode() == expected_revision
        client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).raise_for_status()
        final, events = finish(client, run)
        assert final["status"] == "completed", final["error"]
        assert final["artifacts"][0]["version"] == 2
        assert client.get(final["artifacts"][0]["download_url"]).content == expected_revision
        assert client.get(base["download_url"]).content == original_bytes
        assert any(e["kind"] == "correction.observed" for e in events) is include_error
        assert all(e["data"]["context_budget"]["upper_bound_tokens"] <= 8192 for e in events if e["kind"] == "model.requested")
