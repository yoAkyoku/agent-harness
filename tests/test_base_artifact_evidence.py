"""Selected base versions are readable evidence, not inherited CSV computations."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer, publication_replies
from test_workbench import create_task, finish, tool_reply


@pytest.mark.parametrize("protocol", ["native", "json_tools"])
def test_revision_can_cite_exact_selected_base_without_reopening_its_sources(tmp_path: Path, protocol: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    (source / "other.md").write_bytes(b"No financial data here.\n")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    async def transport(request: httpx.Request) -> httpx.Response:
        response = await peer(request)
        if protocol == "json_tools" and request.url.path == "/v1/chat/completions":
            calls = response.json()["choices"][0]["message"]["tool_calls"]
            content = json.dumps({"calls": [{"name": call["function"]["name"],
                "arguments": json.loads(call["function"]["arguments"])} for call in calls]})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})
        return response

    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol=protocol),
                               model_transport=httpx.MockTransport(transport))) as client:
        original, _ = finish(client, create_task(client, source))
        assert original["status"] == "completed", original["error"]
        base = original["artifacts"][0]
        original_bytes = client.get(base["download_url"]).content
        peer.requests.clear()
        peer.replies = [
            tool_reply("plan_update", {"steps": ["Read selected base and reformat its existing statement."]}, "revision-plan"),
            tool_reply("base_artifact_read", {}, "base-read"),
            tool_reply("artifact_write", {"name": "snapshot.md", "content": "# Reformatted\nRevenue was 42. [base E1]", "citations": ["E1"]}, "revision-write"),
            tool_reply("task_finish", {"summary": "Reformatted."}, "revision-finish"),
        ]
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "Reformat the selected base, preserving its statement.", "sources": ["other.md"],
            "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        revised, events = finish(client, response.json())
        assert revised["status"] == "completed", revised["error"]
        pinned = next(message["content"] for message in peer.requests[0]["messages"]
                      if (message.get("content") or "").startswith("Pinned base artifact"))
        assert json.loads(pinned.split("\n", 1)[1]) == {
            "id": base["id"], "name": "snapshot.md", "sha256": base["sha256"], "version": 1,
        }
        evidence = next(e["data"]["result"] for e in events
                        if e["kind"] == "tool.completed" and e["data"]["tool"] == "base_artifact_read")
        assert evidence == {
            "evidence_id": "E1", "evidence_type": "pinned_artifact", "artifact_id": base["id"],
            "origin_run_id": original["id"], "name": "snapshot.md", "version": 1,
            "sha256": base["sha256"], "text": original_bytes.decode(),
            "line_start": 1, "line_end": len(original_bytes.decode().splitlines()),
        }
        outgoing_result = json.loads(peer.requests[2]["messages"][-1]["content"])
        if protocol == "json_tools":
            outgoing_result = outgoing_result["tool_result"]["value"]
        assert outgoing_result == evidence
        assert "path" not in evidence and "value" not in evidence
        artifact = revised["artifacts"][0]
        assert artifact["version"] == 2 and artifact["parent_artifact_id"] == base["id"]
        result = client.get(artifact["download_url"]).text
        assert result.startswith("# Reformatted\nRevenue was 42. [base E1]")
        assert f"artifact {base['id']}" in result and f"SHA-256 {base['sha256']}" in result
        assert "version 1" in result and original["id"] in result
        assert client.get(base["download_url"]).content == original_bytes
        assert not any(e["kind"] == "tool.completed" and e["data"]["tool"] == "source_read" for e in events)
        if protocol == "native":
            assert "base_artifact_read" in {t["function"]["name"] for t in peer.requests[1]["tools"]}
            assert "base_artifact_read" not in {t["function"]["name"] for t in peer.requests[0]["tools"]}


@pytest.mark.parametrize("tool,arguments,code,with_base", [
    ("base_artifact_read", {}, "unknown_tool", False),
    ("base_artifact_read", {"artifact_id": "f" * 32}, "invalid_arguments", True),
    ("artifact_write", {"name": "bad.md", "content": "Foreign citation", "citations": ["E2"]}, "invalid_evidence", True),
    ("source_read", {"path": "facts.md", "labels": []}, "source_forbidden", True),
    ("number_calculate", {"operation": "ratio_percent", "numerator": "E1", "denominator": "E1"}, "invalid_evidence", True),
])
def test_base_read_does_not_grant_ancestor_ids_paths_or_numeric_operands(
    tmp_path: Path, tool: str, arguments: dict, code: str, with_base: bool,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    (source / "other.csv").write_bytes(b"count\n1\n")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False),
                               model_transport=httpx.MockTransport(peer))) as client:
        original, _ = finish(client, create_task(client, source))
        assert original["status"] == "completed", original["error"]
        base = original["artifacts"][0]
        peer.requests.clear()
        peer.replies = [tool_reply("plan_update", {"steps": ["Check selected scope."]}, "plan")]
        if tool == "number_calculate":
            peer.replies.append(tool_reply("base_artifact_read", {}, "read-base"))
        peer.replies.append(tool_reply(tool, arguments, "rejected"))
        body = {"task": "Check only selected scope.", "sources": ["other.csv"], "limits": {"max_corrections": 0}}
        if with_base:
            body["base_artifact"] = {"run_id": original["id"], "artifact_id": base["id"]}
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json=body)
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())
        assert final["status"] == "failed" and not final["artifacts"]
        assert final["error"]["code"] == code
        if not with_base:
            assert "base_artifact_read" not in {t["function"]["name"] for t in peer.requests[1]["tools"]}


def test_base_cited_draft_survives_real_process_restart_and_publishes_new_version(tmp_path: Path) -> None:
    from draft_process import finish_draft_run, running_draft_service, wait_draft_tool
    from test_recovery import stop

    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    with TestClient(create_app(store, model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        original, _ = finish(client, create_task(client, source))
        assert original["status"] == "completed", original["error"]
        base = original["artifacts"][0]
        old_bytes = client.get(base["download_url"]).content
    replies = [
        tool_reply("plan_update", {"steps": ["Read base and reformat."]}, "plan"),
        tool_reply("base_artifact_read", {}, "base"),
        tool_reply("artifact_draft", {"name": "snapshot.md", "content": "# Revised\nRevenue was 42. [base E1]", "citations": ["E1"]}, "draft"),
    ]
    with running_draft_service(store, replies) as first:
        response = first.client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "Reformat this exact base version.", "sources": ["facts.md"], "publication_workflow": "draft_review",
            "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        revision = response.json()
        draft = wait_draft_tool(first.client, revision["id"], "tool.completed", "artifact_draft")["data"]["result"]
        stop(first.process)
    with running_draft_service(store, [
        tool_reply("artifact_write", {"name": "snapshot.md", "draft_sha256": draft["sha256"], "citations": ["E1"]}, "publish"),
        tool_reply("task_finish", {"summary": "Reformatted."}, "finish"),
    ]) as second:
        checkpoint = second.client.get(f"/api/runs/{revision['id']}/checkpoint").json()
        expected = next(call["result"] for call in checkpoint["confirmed"] if call["tool"] == "base_artifact_read")
        assert expected["artifact_id"] == base["id"] and expected["text"].encode() == old_bytes
        response = second.client.post(f"/api/runs/{revision['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-base-cited-draft"})
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["status"] == "completed", final["error"]
        artifact = final["artifacts"][0]
        assert artifact["version"] == 2 and artifact["parent_artifact_id"] == base["id"]
        assert second.client.get(artifact["download_url"]).text == draft["content"]
        assert second.client.get(base["download_url"]).content == old_bytes
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "base_artifact_read" for e in events)
