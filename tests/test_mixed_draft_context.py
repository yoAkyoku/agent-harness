"""Public replay of a recorded mixed group; control completion is not quality acceptance."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from recorded_citations import bracket_bare_eids, bracket_draft_calls, sha256
from test_completion import CompletionPeer
from test_draft_review import expand_receipt_suffix
from test_json_decisions import json_reply
from test_workbench import create_task, finish


@pytest.mark.parametrize("draft_first", [False, True])
def test_recorded_mixed_inspection_and_draft_fit_without_losing_other_results(tmp_path: Path, draft_first: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    saved = root / "tests/fixtures/recorded/4b-receipt-batch-02/analysis-missing"
    recorded = json.loads((saved / "events.json").read_bytes())
    decisions = [e["data"]["message"]["wire_content"] for e in recorded if e["kind"] == "model.responded"]
    decisions[-1] = bracket_draft_calls(decisions[-1], "metrics.csv")
    if draft_first:
        mixed = json.loads(decisions[-1])
        mixed["calls"].reverse()
        decisions[-1] = json.dumps(mixed, ensure_ascii=False, separators=(",", ":"))
    draft = next(e["data"]["result"] for e in recorded if e["kind"] == "tool.completed" and e["data"]["tool"] == "artifact_draft")
    draft_content = bracket_bare_eids(draft["content"], "metrics.csv")
    draft = {**draft, "content": draft_content, "sha256": sha256(draft_content)}
    original = json.loads((saved / "request.json").read_bytes())
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-missing/metrics.csv").read_bytes())
    peer = CompletionPeer([
        *[httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})
          for text in decisions],
        json_reply("artifact_write", {"name": draft["name"], "draft_sha256": draft["sha256"], "citations": ["E1", "E2"]}),
        json_reply("task_finish", {}),
    ])

    async def select_actual_artifact(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 5:
            body = json.loads(request.content)
            published = json.loads(body["messages"][-1]["content"])["tool_result"]["value"]
            peer.replies[5] = json_reply("task_finish", {"artifact_ids": [published["id"]]})
        return await peer(request)

    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools"),
                               model_transport=httpx.MockTransport(select_actual_artifact))) as client:
        final, events = finish(client, create_task(client, source, task=original["task"], sources=["metrics.csv"],
            limits=original["limits"], publication_workflow="draft_review", completion_workflow="artifact_receipt"))
        assert final["status"] == "completed", (final["error"], len(peer.requests),
            [(e["data"].get("step"), e["data"].get("reason"), e["data"].get("upper_bound_tokens"))
             for e in events if e["kind"] in {"context.pruned", "context.limited"}][-8:])
        request = peer.requests[4]
        candidate = json.loads(request["messages"][-3]["content"])
        calls = candidate["calls"]
        results = [json.loads(message["content"])["tool_result"] for message in request["messages"][-2:]]
        inspected = next(result for result in results if result["name"] == "table_inspect")
        receipt = next(result for result in results if result["name"] == "artifact_draft")
        draft_index = 0 if draft_first else 1
        assert [call["name"] for call in calls] == (["artifact_draft", "table_inspect"] if draft_first else ["table_inspect", "artifact_draft"])
        assert inspected["call"] == 1 - draft_index
        durable_read = next(e["data"]["result"] for e in reversed(events) if e["kind"] == "tool.completed" and e["data"]["tool"] == "table_inspect")
        assert {k: v for k, v in inspected["value"].items() if k != "aggregatable_columns"} == {
            k: v for k, v in durable_read.items() if k != "aggregatable_columns"}
        assert receipt["call"] == draft_index
        encoded = receipt["value"]
        assert encoded["content"]["call"] == draft_index
        complete = calls[draft_index]["arguments"]["content"] + expand_receipt_suffix(encoded["content"]["suffix"])
        assert complete == draft["content"]
        assert hashlib.sha256(complete.encode()).hexdigest() == encoded["sha256"] == draft["sha256"]
        assert client.get(final["artifacts"][0]["download_url"]).content == complete.encode()
        assert "2023-10-27" in complete and "abnormal_orders" in complete  # preserved defects, not an accepted answer
        assert any(e["kind"] == "context.pruned" and e["data"].get("reason") == "draft_receipt_encoding" for e in events)
        state_message = next(message["content"] for message in peer.requests[5]["messages"]
                             if message["role"] == "system" and message["content"].startswith("Saved executor state"))
        state = json.loads(state_message.split("\n", 1)[1])
        restored = dict(state["verified_evidence"]["E4"])
        reference = restored.pop("source_ref", None)
        if reference is not None:
            restored.update(state["evidence_sources"][reference])
        assert {k: v for k, v in restored.items() if k != "aggregatable_columns"} == {
            k: v for k, v in durable_read.items() if k != "aggregatable_columns"}
        assert any(e["kind"] == "context.pruned" and e["data"].get("reason") == "published_draft" for e in events)
        assert all(e["data"]["context_budget"]["upper_bound_tokens"] <= 8192 for e in events if e["kind"] == "model.requested")


def test_published_mixed_group_retains_its_read_across_a_real_process_restart(tmp_path: Path) -> None:
    from draft_process import finish_draft_run, running_draft_service, wait_draft_tool
    from test_recovery import create_process_task, stop

    root = Path(__file__).resolve().parents[1]
    saved = root / "tests/fixtures/recorded/4b-receipt-batch-02/analysis-missing"
    recorded = json.loads((saved / "events.json").read_bytes())
    original_request = json.loads((saved / "request.json").read_bytes())
    draft = next(e["data"]["result"] for e in recorded if e["kind"] == "tool.completed" and e["data"]["tool"] == "artifact_draft")
    draft_content = bracket_bare_eids(draft["content"], "metrics.csv")
    draft = {**draft, "content": draft_content, "sha256": sha256(draft_content)}
    replies = [httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": bracket_draft_calls(e["data"]["message"]["wire_content"], "metrics.csv")}}]})
               for e in recorded if e["kind"] == "model.responded"]
    replies.append(json_reply("artifact_write", {"name": draft["name"], "draft_sha256": draft["sha256"], "citations": ["E1", "E2"]}))
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-missing/metrics.csv").read_bytes())
    store = tmp_path / "store"
    with running_draft_service(store, replies, decision_protocol="json_tools") as first:
        original = create_process_task(first.client, source, task=original_request["task"], sources=["metrics.csv"],
            publication_workflow="draft_review", limits=original_request["limits"])
        artifact = wait_draft_tool(first.client, original["id"], "tool.completed", "artifact_write")["data"]["result"]
        stop(first.process)
    final_reply = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": '{"reply":"Synthetic control replay complete."}'}}]})
    with running_draft_service(store, [final_reply], decision_protocol="json_tools") as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        expected = next(call["result"] for call in checkpoint["confirmed"] if call["tool"] == "table_inspect" and call["result"]["evidence_id"] == "E4")
        assert any(message["role"] == "tool" and json.loads(message["content"]).get("evidence_id") == "E4"
                   for message in checkpoint["messages"])
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-published-mixed-group"})
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["status"] == "completed", final["error"]
        assert [e["data"]["tool"] for e in events if e["kind"] == "tool.dispatched"] == ["task_finish"]
        assert not final["artifacts"] and [item["id"] for item in final["inherited_artifacts"]] == [artifact["id"]]
        assert second.client.get(artifact["download_url"]).content == draft["content"].encode()
        outgoing = json.loads(second.model_requests.read_bytes())[0]
        state_text = next(message["content"] for message in outgoing["messages"]
                          if message["role"] == "system" and message["content"].startswith("Saved executor state"))
        state = json.loads(state_text.split("\n", 1)[1])
        restored = dict(state["verified_evidence"]["E4"])
        reference = restored.pop("source_ref", None)
        if reference is not None:
            restored.update(state["evidence_sources"][reference])
        assert {k: v for k, v in restored.items() if k != "aggregatable_columns"} == {
            k: v for k, v in expected.items() if k != "aggregatable_columns"}
