import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_approval import pending_approval
from test_completion import CompletionPeer, publication_replies
from test_workbench import create_task, finish, tool_reply


def expand_receipt_suffix(suffix: str | dict) -> str:
    if isinstance(suffix, str):
        return suffix
    return "".join(part if isinstance(part, str) else suffix["refs"][part] for part in suffix["parts"])


def test_draft_is_not_an_artifact_and_later_publication_requires_original_approval(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Read, draft, review and publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_draft", candidate, "draft"),
        tool_reply("artifact_write", candidate, "write"),
        tool_reply("task_finish", {"summary": "Snapshot saved."}, "finish"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source, publication_workflow="draft_review", require_publish_approval=True)
        approval = pending_approval(client, run)
        waiting = client.get(f"/api/runs/{run['id']}").json()
        assert waiting["status"] == "waiting_approval" and waiting["artifacts"] == []
        assert waiting["policy"]["publication_workflow"] == "draft_review"
        assert len(peer.requests) == 4
        draft_message = peer.requests[-1]["messages"][-1]
        assert draft_message["role"] == "tool" and draft_message["tool_call_id"] == "draft"
        draft = json.loads(draft_message["content"])
        assert draft["published"] is False and draft["name"] == "snapshot.md"
        assert draft["content"] == approval["arguments"]["content"]
        assert "facts.md:L1-L1" in draft["content"]
        response = client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={
            "decision": "approve", "parameters_sha256": approval["parameters_sha256"],
        })
        assert response.status_code == 200, response.text
        final, events = finish(client, run)
        assert final["status"] == "completed", final["error"]
        assert final["usage"] == {"model_calls": 5, "tool_calls": 5}
        assert len(final["artifacts"]) == 1
        artifact = final["artifacts"][0]
        assert artifact["sha256"] == draft["sha256"]
        assert client.get(artifact["download_url"]).text == draft["content"]
        kinds = [event["kind"] for event in events]
        draft_position = next(index for index, event in enumerate(events)
                              if event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_draft")
        assert draft_position < kinds.index("approval.requested") < kinds.index("approval.approved")
        assert kinds.index("approval.consumed") < kinds.index("artifact.published") < kinds.index("run.completed")


@pytest.mark.parametrize("arguments", [{}, {"name": "snapshot.md", "content": "A candidate."}])
def test_direct_policy_rejects_draft_even_when_unadvertised_or_malformed(tmp_path: Path, arguments: dict) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer([tool_reply("artifact_draft", arguments, "draft")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source))
        assert final["status"] == "failed" and final["error"]["code"] == "tool_not_permitted", final
        assert not final["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" for event in events)
        assert all(tool["function"]["name"] != "artifact_draft" for tool in peer.requests[0]["tools"])


@pytest.mark.parametrize("attempt", ["without_draft", "same_decision", "changed_name", "changed_content"])
def test_unreviewed_candidate_never_reaches_publication(tmp_path: Path, attempt: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    replies = [tool_reply("plan_update", {"steps": ["Read and draft"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read")]
    draft = tool_reply("artifact_draft", candidate, "draft")
    if attempt == "same_decision":
        calls = draft.json()["choices"][0]["message"]["tool_calls"]
        calls += tool_reply("artifact_write", candidate, "write").json()["choices"][0]["message"]["tool_calls"]
        replies.append(httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}))
    else:
        if attempt != "without_draft":
            replies.append(draft)
        proposed = dict(candidate)
        if attempt == "changed_name":
            proposed["name"] = "other.md"
        if attempt == "changed_content":
            proposed["content"] = "Revenue was 43. [facts.md E1]"
        replies.append(tool_reply("artifact_write", proposed, "write"))
    peer = CompletionPeer(replies)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, publication_workflow="draft_review"))
        assert final["status"] == "failed" and final["error"]["code"] == "unreviewed_artifact", final
        assert not final["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)


@pytest.mark.parametrize("ending", ["plaintext", "finish", "empty"])
def test_unpublished_draft_cannot_complete_the_run(tmp_path: Path, ending: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    replies = [tool_reply("plan_update", {"steps": ["Read and draft"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
               tool_reply("artifact_draft", {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "draft")]
    replies.append(tool_reply("task_finish", {"summary": "Done."}, "finish") if ending == "finish" else
                   httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "" if ending == "empty" else "Done."}}]}))
    peer = CompletionPeer(replies)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, publication_workflow="draft_review"))
        assert final["status"] == "failed" and final["error"]["code"] == ("model_protocol_error" if ending == "empty" else "unpublished_draft"), final
        assert not final["artifacts"]
        assert not any(event["kind"] == "run.completed" for event in events)


def test_draft_uses_the_original_tool_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Read and draft"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_draft", candidate, "draft"),
        tool_reply("artifact_write", candidate, "write"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, _ = finish(client, create_task(client, source, publication_workflow="draft_review", limits={"max_tool_calls": 3}))
        assert final["error"]["code"] == "tool_limit_exceeded" and final["status"] == "failed"
        assert final["usage"] == {"model_calls": 4, "tool_calls": 3}
        assert not final["artifacts"]


def test_default_direct_submission_is_equivalent_but_changed_workflow_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source, submission_key="default-workflow-key")
        final, _ = finish(client, run)
        assert final["status"] == "completed", final
        body = {"task": "Save a source snapshot.", "sources": ["facts.md"], "submission_key": "default-workflow-key",
                "publication_workflow": "direct"}
        response = client.post(f"/api/sessions/{run['session_id']}/runs", json=body)
        assert response.status_code == 202 and response.json()["id"] == run["id"]
        body["publication_workflow"] = "draft_review"
        assert client.post(f"/api/sessions/{run['session_id']}/runs", json=body).status_code == 409
        assert len(client.get(f"/api/sessions/{run['session_id']}/runs").json()) == 1
        assert len(peer.requests) == 4


def test_reused_publication_clears_reused_draft_across_two_real_process_deaths(tmp_path: Path) -> None:
    from draft_process import finish_draft_run, running_draft_service, wait_draft_tool
    from test_recovery import create_process_task, stop

    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    with running_draft_service(store, [
        tool_reply("plan_update", {"steps": ["Read, draft and publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_draft", candidate, "draft"),
        tool_reply("artifact_write", candidate, "write"),
    ]) as first:
        original = create_process_task(first.client, source, publication_workflow="draft_review")
        published = wait_draft_tool(first.client, original["id"], "tool.completed", "artifact_write")["data"]["result"]
        original_bytes = first.client.get(published["download_url"]).content
        stop(first.process)
    with running_draft_service(store, [tool_reply("artifact_draft", candidate, "draft-again"),
                                       tool_reply("artifact_write", candidate, "write-again")]) as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "reuse-draft-and-publication",
        })
        assert response.status_code == 202, response.text
        resumed = response.json()
        wait_draft_tool(second.client, resumed["id"], "tool.reused", "artifact_write")
        assert second.client.get(f"/api/runs/{resumed['id']}").json()["artifacts"] == []
        stop(second.process)
    with running_draft_service(store, [tool_reply("task_finish", {"summary": "Saved result retained."}, "finish")]) as third:
        checkpoint = third.client.get(f"/api/runs/{resumed['id']}/checkpoint").json()
        response = third.client.post(f"/api/runs/{resumed['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "finish-reused-publication",
        })
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(third.client, response.json()["id"])
        assert final["status"] == "completed", final["error"]
        assert final["artifacts"] == [] and len(final["inherited_artifacts"]) == 1
        assert third.client.get(published["download_url"]).content == original_bytes
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] in {"artifact_draft", "artifact_write"} for event in events)


def test_numeric_feedback_retries_the_rejected_draft_before_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes(b"orders\n1\n2\n")
    first = {"name": "report.md", "content": "1 + 2 = 3 [metrics.csv E1]", "citations": ["E1"]}
    repaired = {"name": "report.md", "content": "1 + 2 = 3 [metrics.csv E2]", "citations": ["E1", "E2"]}
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Inspect, calculate, draft and publish"]}, "plan"),
        tool_reply("table_inspect", {"path": "metrics.csv"}, "inspect"),
        tool_reply("artifact_draft", first, "bad-draft"),
        tool_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}, "sum"),
        tool_reply("artifact_draft", repaired, "repaired-draft"),
        tool_reply("artifact_write", repaired, "publish"),
        tool_reply("task_finish", {"summary": "Calculated total 3 saved."}, "finish"),
    ])
    async def follow_feedback(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 4:
            body = json.loads(request.content)
            feedback = [json.loads(message["content"]) for message in body["messages"]
                        if message["role"] == "tool" and json.loads(message["content"]).get("ok") is False][-1]
            retry_tool = "artifact_write" if "Retry artifact_write" in feedback["error"]["message"] else "artifact_draft"
            peer.replies[4] = tool_reply(retry_tool, repaired, "repaired-draft")
        return await peer(request)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(follow_feedback))) as client:
        final, events = finish(client, create_task(client, source, sources=["metrics.csv"], publication_workflow="draft_review"))
        assert final["status"] == "completed", (final["error"], final["usage"],
            [event["data"].get("step") for event in events if event["kind"] == "context.limited"])
        correction = next(event["data"] for event in events if event["kind"] == "correction.observed")
        assert correction["failed_call_ids"] == ["bad-draft"] and correction["successful_call_id"] == "repaired-draft"
        assert final["usage"] == {"model_calls": 7, "tool_calls": 7}
        assert "sum(orders) = 3" in client.get(final["artifacts"][0]["download_url"]).text


@pytest.mark.parametrize("decision", ["deny", "cancel"])
def test_draft_waiting_publication_can_be_denied_or_cancelled(tmp_path: Path, decision: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Read, draft and publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_draft", candidate, "draft"),
        tool_reply("artifact_write", candidate, "write"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source, publication_workflow="draft_review", require_publish_approval=True)
        approval = pending_approval(client, run)
        url = f"/api/runs/{run['id']}/approvals/{approval['id']}"
        if decision == "cancel":
            assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 202
        else:
            assert client.post(url, json={"decision": "deny", "parameters_sha256": approval["parameters_sha256"]}).status_code == 200
        final, events = finish(client, run)
        assert final["status"] == ("cancelled" if decision == "cancel" else "failed")
        assert final["artifacts"] == []
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)
        assert client.post(url, json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 409
        assert client.get(f"/api/runs/{run['id']}").json()["artifacts"] == []


@pytest.mark.parametrize("scenario", ["reuse_latest", "unpublished_after_artifact"])
def test_durable_latest_draft_controls_resume(tmp_path: Path, scenario: str) -> None:
    from draft_process import finish_draft_run, running_draft_service, wait_draft_tool
    from test_recovery import create_process_task, stop

    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    a = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    b = {**a, "content": "Source reports revenue of 42. [facts.md E1]"}
    replies = [tool_reply("plan_update", {"steps": ["Read, draft and publish"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
               tool_reply("artifact_draft", a, "draft-a")]
    if scenario == "unpublished_after_artifact":
        replies += [tool_reply("artifact_write", a, "write-a"), tool_reply("artifact_draft", b, "draft-b")]
        last_kind = "tool.completed"
    else:
        replies += [tool_reply("artifact_draft", b, "draft-b")]
        last_kind = "tool.completed"
    with running_draft_service(store, replies) as first:
        original = create_process_task(first.client, source, publication_workflow="draft_review")
        wait_draft_tool(first.client, original["id"], last_kind, "artifact_draft", "draft-b")
        stop(first.process)
    if scenario == "reuse_latest":
        with running_draft_service(store, [tool_reply("artifact_draft", a, "reuse-a")]) as middle:
            checkpoint = middle.client.get(f"/api/runs/{original['id']}/checkpoint").json()
            response = middle.client.post(f"/api/runs/{original['id']}/resume", json={
                "checkpoint_sha256": checkpoint["sha256"], "submission_key": "reuse-a-after-b",
            })
            assert response.status_code == 202, response.text
            original = response.json()
            wait_draft_tool(middle.client, original["id"], "tool.reused", "artifact_draft", "reuse-a")
            stop(middle.process)
    continuation = [tool_reply("task_finish", {"summary": "Saved."}, "finish")]
    if scenario == "reuse_latest":
        continuation.insert(0, tool_reply("artifact_write", a, "write-a"))
    with running_draft_service(store, continuation) as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-latest-draft",
        })
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["policy"]["publication_workflow"] == "draft_review"
        if scenario == "reuse_latest":
            assert final["status"] == "completed", final["error"]
            assert len(final["artifacts"]) == 1
            assert second.client.get(final["artifacts"][0]["download_url"]).text.startswith(a["content"])
        else:
            assert final["status"] == "failed" and final["error"]["code"] == "unpublished_draft", final
            assert final["artifacts"] == [] and len(final["inherited_artifacts"]) == 1
            assert not any(event["kind"] == "run.completed" for event in events)


def test_reconciled_publication_clears_draft_without_rewriting(tmp_path: Path) -> None:
    from draft_process import finish_draft_run, running_draft_service
    from test_recovery import create_process_task, stop, wait_event

    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    replies = [tool_reply("plan_update", {"steps": ["Read, draft and publish"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
               tool_reply("artifact_draft", candidate, "draft"),
               tool_reply("artifact_write", candidate, "write")]
    with running_draft_service(store, replies, hold_publication=True) as first:
        original = create_process_task(first.client, source, publication_workflow="draft_review")
        held = wait_event(first.client, original["id"], "validation.tool_held")
        assert held["data"]["phase"] == "after_publish_before_result"
        saved = first.client.get(f"/api/runs/{original['id']}").json()
        artifact = saved["artifacts"][0]
        raw = first.client.get(artifact["download_url"]).content
        stop(first.process)
    with running_draft_service(store, [tool_reply("task_finish", {"summary": "Saved."}, "finish")]) as second:
        events = second.client.get(f"/api/runs/{original['id']}/events").json()["events"]
        assert any(event["kind"] == "tool.reconciled" and event["data"]["call_id"] == "write" for event in events)
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "finish-reconciled-draft",
        })
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["status"] == "completed", final["error"]
        assert final["artifacts"] == [] and len(final["inherited_artifacts"]) == 1
        assert second.client.get(artifact["download_url"]).content == raw
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)


def test_ancestor_step_ten_draft_can_publish_at_resumed_step_one(tmp_path: Path) -> None:
    from draft_process import finish_draft_run, running_draft_service, wait_draft_tool
    from test_recovery import create_process_task, stop

    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    replies = [tool_reply("plan_update", {"steps": ["Read and publish"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read")]
    replies.extend(tool_reply("plan_update", {"steps": [f"Review {index} and publish"]}, f"plan-{index}") for index in range(7))
    replies.append(tool_reply("artifact_draft", candidate, "late-draft"))
    with running_draft_service(store, replies) as first:
        original = create_process_task(first.client, source, publication_workflow="draft_review")
        wait_draft_tool(first.client, original["id"], "tool.completed", "artifact_draft", "late-draft")
        events = first.client.get(f"/api/runs/{original['id']}/events").json()["events"]
        decisions = [event for event in events if event["kind"] == "model.responded"]
        assert len(decisions) == 10
        stop(first.process)
    with running_draft_service(store, [tool_reply("artifact_write", candidate, "write"),
                                       tool_reply("task_finish", {"summary": "Saved."}, "finish")]) as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "publish-ancestor-step-ten",
        })
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["status"] == "completed", final["error"]
        assert final["usage"] == {"model_calls": 2, "tool_calls": 2}
        assert next(event["data"]["step"] for event in events if event["kind"] == "model.requested") == 1
        assert len(final["artifacts"]) == 1


def test_unpublished_draft_is_not_pruned_to_bypass_context_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. " * 400 + "[facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Read and draft"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_draft", candidate, "draft"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, publication_workflow="draft_review"))
        assert final["status"] == "failed" and final["error"]["code"] == "context_limit_exceeded", final
        assert final["usage"] == {"model_calls": 3, "tool_calls": 3}
        assert final["artifacts"] == []
        assert any(event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_draft" for event in events)
        assert not any(event["kind"] == "context.pruned" and "draft" in event["data"].get("call_ids", [])
                       and event["data"].get("reason") != "draft_receipt_encoding" for event in events)


def test_pre_workflow_submission_key_survives_upgrade(tmp_path: Path) -> None:
    import zipfile
    from draft_process import finish_draft_run, running_draft_service
    from legacy import legacy_source_zip
    from test_recovery import create_process_task

    root = Path(__file__).resolve().parents[1]
    baseline = "97f0822"
    archive = tmp_path / "pre-workflow.zip"
    archive.write_bytes(legacy_source_zip(baseline))
    previous = tmp_path / "previous"
    with zipfile.ZipFile(archive) as saved:
        saved.extractall(previous)
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    replies = publication_replies() + [tool_reply("task_finish", {"summary": "Saved."}, "finish")]
    with running_draft_service(store, replies, package_root=previous) as old:
        run = create_process_task(old.client, source, submission_key="pre-workflow-key")
        final, original_events = finish_draft_run(old.client, run["id"])
        assert final["status"] == "completed", final["error"]
        assert "publication_workflow" not in final["policy"]
    with running_draft_service(store, []) as current:
        body = {"task": "Save a source snapshot.", "sources": ["facts.md"], "submission_key": "pre-workflow-key"}
        url = f"/api/sessions/{run['session_id']}/runs"
        for explicit in (False, True):
            if explicit:
                body["publication_workflow"] = "direct"
            response = current.client.post(url, json=body)
            assert response.status_code == 202 and response.json()["id"] == run["id"], response.text
        body["publication_workflow"] = "draft_review"
        assert current.client.post(url, json=body).status_code == 409
        assert len(current.client.get(url).json()) == 1
        assert current.client.get(f"/api/runs/{run['id']}/events").json()["events"] == original_events


@pytest.mark.parametrize("limit,expected,model_calls", [
    ({"max_steps": 3}, "step_limit_exceeded", 3),
    ({"max_corrections": 1}, "correction_limit_exceeded", 3),
    ({"max_corrections": 0}, "invalid_evidence", 2),
])
def test_draft_preserves_decision_and_correction_limits(tmp_path: Path, limit: dict, expected: str, model_calls: int) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Unsupported claim", "citations": ["E99"]}
    if "max_steps" in limit:
        replies = [tool_reply("plan_update", {"steps": ["Read and draft"]}, "plan"),
                   tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
                   tool_reply("artifact_draft", {**candidate, "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "draft")]
    else:
        replies = [tool_reply("plan_update", {"steps": ["Draft"]}, "plan"),
                   tool_reply("artifact_draft", candidate, "bad"), tool_reply("artifact_draft", candidate, "still-bad")]
    peer = CompletionPeer(replies)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, publication_workflow="draft_review", limits=limit))
        assert final["status"] == "failed" and final["error"]["code"] == expected, final
        assert final["usage"]["model_calls"] == model_calls
        assert final["artifacts"] == []
        if "max_steps" not in limit:
            assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_draft" for event in events)


@pytest.mark.parametrize("batch", ["4b-batch-03", "4b-batch-04"])
@pytest.mark.parametrize("invalid_name", [False, True])
def test_recorded_full_draft_uses_lossless_context_and_original_validation(tmp_path: Path, invalid_name: bool, batch: str) -> None:
    import hashlib
    root = Path(__file__).resolve().parents[1]
    recorded = json.loads((root / f"tests/fixtures/recorded/{batch}/analysis-standard/events.json").read_text(encoding="utf-8"))
    messages = [event["data"]["message"] for event in recorded if event["kind"] == "model.responded"]
    candidate = json.loads(messages[-1]["tool_calls"][0]["function"]["arguments"])
    publication = {**candidate, "name": "x" * 101 + ".md"} if invalid_name else candidate
    peer = CompletionPeer([httpx.Response(200, json={"choices": [{"message": message}]}) for message in messages] +
                          [tool_reply("artifact_write", publication, "publish"), tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-standard/metrics.csv").read_bytes())
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        task = json.loads((root / "evaluation/slice-09/cases.json").read_text(encoding="utf-8"))["cases"][0]["task"]
        final, saved = finish(client, create_task(client, source, task=task, sources=["metrics.csv"], publication_workflow="draft_review", limits={"max_corrections": 0}))
        assert len(peer.requests) >= len(messages) + 1, (final["error"], [(e["data"].get("reason"), e["data"]["upper_bound_tokens"]) for e in saved if e["kind"] in {"context.limited", "context.pruned"}][-3:])
        request = peer.requests[len(messages)]
        receipt = json.loads(request["messages"][-1]["content"])
        assert isinstance(receipt["content"]["suffix"], dict)
        assert len(receipt["content"]["suffix"]["refs"]) == 1
        call = request["messages"][-2]["tool_calls"][0]
        assert receipt["content"]["call"] == 0
        assert request["messages"][-1]["tool_call_id"] == call["id"]
        body = json.loads(call["function"]["arguments"])["content"]
        effective = body + expand_receipt_suffix(receipt["content"]["suffix"])
        assert hashlib.sha256(effective.encode()).hexdigest() == receipt["sha256"]
        durable = next(event["data"]["result"] for event in saved if event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_draft")
        assert durable == {**receipt, "content": effective}
        budget = next(event["data"]["context_budget"] for event in saved if event["kind"] == "model.requested" and event["data"]["step"] == len(messages) + 1)
        assert budget["input_utf8_bytes"] == len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
        assert budget["upper_bound_tokens"] <= budget["capacity_tokens"] == 8192
        inspected = next(event["data"]["result"] for event in saved
                         if event["kind"] == "tool.completed" and event["data"]["tool"] == "table_inspect")
        assert inspected["aggregatable_columns"] == ["week", "orders", "exceptions"]
        # Lossy hint removal is a last resort: it must only happen under budget
        # pressure, and when it happens the hints must really be gone.
        hints_pruned = any(event["kind"] == "context.pruned" and event["data"].get("reason") == "inspection_capability_hints"
                           for event in saved)
        hints_sent = "aggregatable_columns" in "\n".join(str(message.get("content") or "") for message in request["messages"])
        assert hints_pruned != hints_sent
        assert any(event["kind"] == "context.pruned" and event["data"].get("reason") == "draft_receipt_encoding" for event in saved)
        writer = next(tool for tool in request["tools"] if tool["function"]["name"] == "artifact_write")
        bounds_pruned = any(event["kind"] == "context.pruned" and event["data"].get("reason") == "tool_schema_bounds"
                            for event in saved)
        assert ("maxLength" in writer["function"]["parameters"]["properties"]["name"]) != bounds_pruned
        assert writer["function"]["parameters"]["additionalProperties"] is False
        # Whether or not the advertised schema kept its bounds, the server still enforces them.
        if invalid_name:
            assert final["status"] == "failed" and final["error"]["code"] == "invalid_arguments", final
            assert not final["artifacts"]
            assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in saved)
        else:
            assert final["status"] == "completed", final["error"]
            assert final["usage"] == {"model_calls": len(messages) + 2, "tool_calls": sum(len(message["tool_calls"]) for message in messages) + 2}
            assert client.get(final["artifacts"][0]["download_url"]).text == effective


def test_large_ancestor_draft_receipt_can_be_reconstructed_from_resumed_request(tmp_path: Path) -> None:
    import hashlib
    from draft_process import finish_draft_run, running_draft_service, wait_draft_tool
    from test_recovery import create_process_task, stop
    root = Path(__file__).resolve().parents[1]
    recorded = json.loads((root / "tests/fixtures/recorded/4b-batch-03/analysis-standard/events.json").read_text(encoding="utf-8"))
    messages = [event["data"]["message"] for event in recorded if event["kind"] == "model.responded"]
    candidate = json.loads(messages[-1]["tool_calls"][0]["function"]["arguments"])
    replies = [httpx.Response(200, json={"choices": [{"message": message}]}) for message in messages]
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-standard/metrics.csv").read_bytes())
    task = json.loads((root / "evaluation/slice-09/cases.json").read_text(encoding="utf-8"))["cases"][0]["task"]
    store = tmp_path / "store"
    with running_draft_service(store, replies) as first:
        original = create_process_task(first.client, source, task=task, sources=["metrics.csv"], publication_workflow="draft_review")
        draft = wait_draft_tool(first.client, original["id"], "tool.completed", "artifact_draft")["data"]["result"]
        stop(first.process)
    with running_draft_service(store, [tool_reply("artifact_write", candidate, "publish"),
                                       tool_reply("task_finish", {"summary": "Saved."}, "finish")]) as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-full-sized-draft",
        })
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["status"] == "completed", (final["error"], [(e["data"].get("step"), e["data"].get("reason"), e["data"]["upper_bound_tokens"]) for e in events if e["kind"] in {"context.limited", "context.pruned"}][-4:])
        outgoing = json.loads(second.model_requests.read_text(encoding="utf-8"))[0]
        receipt = json.loads(outgoing["messages"][-1]["content"])
        assert isinstance(receipt["content"], dict)
        call = outgoing["messages"][-2]["tool_calls"][0]
        assert receipt["content"]["call"] == 0
        assert outgoing["messages"][-1]["tool_call_id"] == call["id"]
        effective = json.loads(call["function"]["arguments"])["content"] + expand_receipt_suffix(receipt["content"]["suffix"])
        assert draft == {**receipt, "content": effective}
        budget = next(event["data"]["context_budget"] for event in events if event["kind"] == "model.requested")
        assert budget["input_utf8_bytes"] == len(json.dumps(outgoing, ensure_ascii=False, separators=(",", ":")).encode())
        assert budget["upper_bound_tokens"] <= budget["capacity_tokens"] == 8192
        assert hashlib.sha256(effective.encode()).hexdigest() == draft["sha256"]
        assert second.client.get(final["artifacts"][0]["download_url"]).text == effective


def test_shared_suffix_keeps_distinct_source_versions_and_line_ranges(tmp_path: Path) -> None:
    import hashlib
    source = tmp_path / "source"
    source.mkdir()
    raw = {"a.csv": b"orders\n1\n2\n", "b.csv": b"orders\n3\n4\n"}
    for name, content in raw.items():
        (source / name).write_bytes(content)
    candidate = {"name": "report.md", "content": "Sources: a.csv [E1], b.csv [E4].\n" + "Source evidence is available. " * 10,
                 "citations": ["E1", "E2", "E3", "E4", "E5", "E6"]}
    replies = [tool_reply("plan_update", {"steps": ["Read source evidence and draft"]}, "plan")]
    for index, name in enumerate(raw):
        replies += [tool_reply("source_read", {"path": name, "labels": []}, f"read-{index}"),
                    tool_reply("table_inspect", {"path": name}, f"inspect-{index}"),
                    tool_reply("table_aggregate", {"path": name, "column": "orders", "operation": "sum"}, f"sum-{index}")]
    replies += [tool_reply("artifact_draft", candidate, "draft"), tool_reply("artifact_write", candidate, "write"),
                tool_reply("task_finish", {"summary": "Saved."}, "finish")]
    peer = CompletionPeer(replies)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=list(raw), publication_workflow="draft_review"))
        assert final["status"] == "completed", (final["error"], [(e["data"]["step"], e["data"]["upper_bound_tokens"]) for e in events if e["kind"] == "context.limited"])
        outgoing = peer.requests[8]
        receipt = json.loads(outgoing["messages"][-1]["content"])
        suffix = receipt["content"]["suffix"]
        assert isinstance(suffix, dict)
        assert set(suffix["refs"]) == {f"{name}:L2-L3 SHA-256 {hashlib.sha256(content).hexdigest()}" for name, content in raw.items()}
        expanded = expand_receipt_suffix(suffix)
        assert "[E1] a.csv:L1-L3" in expanded and "[E4] b.csv:L1-L3" in expanded
        assert "[E2] a.csv:L2-L3" in expanded and "[E3] a.csv:L2-L3" in expanded
        assert "[E5] b.csv:L2-L3" in expanded and "[E6] b.csv:L2-L3" in expanded
        effective = candidate["content"] + expanded
        durable = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_draft")
        assert durable == {**receipt, "content": effective}
        assert hashlib.sha256(effective.encode()).hexdigest() == durable["sha256"]
        assert client.get(final["artifacts"][0]["download_url"]).text == effective


def test_single_source_suffix_remains_plain_when_sharing_cannot_save_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Source: facts.md [E1].\n" + "Data stays source-bound. " * 85, "citations": ["E1"]}
    peer = CompletionPeer([tool_reply("plan_update", {"steps": ["Read and draft"]}, "plan"),
                           tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
                           tool_reply("artifact_draft", candidate, "draft"), tool_reply("artifact_write", candidate, "publish"),
                           tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, publication_workflow="draft_review"))
        assert final["status"] == "completed", (final["error"], [(e["data"]["step"], e["data"]["upper_bound_tokens"]) for e in events if e["kind"] == "context.limited"])
        receipt = json.loads(peer.requests[3]["messages"][-1]["content"])
        assert isinstance(receipt["content"], dict) and isinstance(receipt["content"]["suffix"], str)
        effective = candidate["content"] + expand_receipt_suffix(receipt["content"]["suffix"])
        durable = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_draft")
        assert durable == {**receipt, "content": effective}
        assert client.get(final["artifacts"][0]["download_url"]).text == effective


def test_filename_hash_text_is_not_mistaken_for_the_complete_source_location(tmp_path: Path) -> None:
    import hashlib
    source = tmp_path / "source"
    source.mkdir()
    prefix = "report SHA-256 " + "a" * 64
    raw = {prefix + ";a.md": b"Revenue was 42.\n", prefix + ";b.md": b"Revenue was 43.\n"}
    names = list(raw)
    for name, content in raw.items():
        (source / name).write_bytes(content)
    candidate = {"name": "report.md", "content": "Read both selected sources [E1] [E2].\n" + "Scoped source facts. " * 50,
                 "citations": ["E1", "E2", "E3", "E4"]}
    replies = [tool_reply("plan_update", {"steps": ["Read both selected sources and draft"]}, "plan")]
    replies += [tool_reply("source_read", {"path": name, "labels": []}, f"read-{index}") for index, name in enumerate(names * 2)]
    replies += [tool_reply("artifact_draft", candidate, "draft"), tool_reply("artifact_write", candidate, "publish"),
                tool_reply("task_finish", {"summary": "Saved."}, "finish")]
    peer = CompletionPeer(replies)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=names, publication_workflow="draft_review"))
        assert final["status"] == "completed", (final["error"], [(e["data"]["step"], e["data"]["upper_bound_tokens"]) for e in events if e["kind"] == "context.limited"])
        receipt = json.loads(peer.requests[6]["messages"][-1]["content"])
        suffix = receipt["content"]["suffix"]
        assert isinstance(suffix, dict)
        assert set(suffix["refs"]) == {f"{name}:L1-L1 SHA-256 {hashlib.sha256(content).hexdigest()}" for name, content in raw.items()}
        assert prefix not in suffix["refs"]
        effective = candidate["content"] + expand_receipt_suffix(suffix)
        durable = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_draft")
        assert durable == {**receipt, "content": effective}
        assert client.get(final["artifacts"][0]["download_url"]).text == effective
