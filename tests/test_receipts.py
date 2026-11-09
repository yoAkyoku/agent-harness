"""Model-selected completion receipts through public HTTP and model transport."""
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_approval import pending_approval
from test_completion import CompletionPeer
from test_json_decisions import json_reply, streamed_json_reply
from test_workbench import create_task, finish, tool_reply


@pytest.mark.parametrize("protocol", ["native", "json_tools"])
def test_model_selects_confirmed_artifact_after_approval_for_a_receipt(tmp_path: Path, protocol: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    def reply(name: str, arguments: dict) -> httpx.Response:
        return json_reply(name, arguments) if protocol == "json_tools" else tool_reply(name, arguments, name)
    peer = CompletionPeer([reply("plan_update", {"steps": ["Read and publish"]}),
                           reply("source_read", {"path": "facts.md", "labels": []}),
                           reply("artifact_draft", candidate), reply("artifact_write", candidate),
                           reply("task_finish", {})])
    chosen: list[str] = []
    async def select_published_artifact(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 4:
            body = json.loads(request.content)
            result = json.loads(body["messages"][-1]["content"])
            if protocol == "json_tools": result = result["tool_result"]["value"]
            chosen.append(result["id"])
            peer.replies[4] = (httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"reply": {"artifact_ids": chosen}})}}]})
                if protocol == "json_tools" else tool_reply("task_finish", {"artifact_ids": chosen}, "finish"))
        return await peer(request)
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol=protocol)
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(select_published_artifact))) as client:
        run = create_task(client, source, completion_workflow="artifact_receipt", publication_workflow="draft_review",
                          require_publish_approval=True, submission_key="receipt-submit")
        approval = pending_approval(client, run)
        assert len(peer.requests) == 4 and not chosen
        if protocol == "json_tools":
            assert all('"reply"' not in json.dumps(r["response_format"]) for r in peer.requests)
        else:
            assert all("task_finish" not in {t["function"]["name"] for t in r["tools"]} for r in peer.requests)
        assert client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={
            "decision": "approve", "parameters_sha256": approval["parameters_sha256"]}).status_code == 200
        final, events = finish(client, run)
        assert final["status"] == "completed", final["error"]
        assert final["policy"]["completion_workflow"] == "artifact_receipt"
        assert final["result"] == "已發布：snapshot.md。"
        assert chosen == [final["artifacts"][0]["id"]]
        assert final["usage"] == {"model_calls": 5, "tool_calls": 5}
        assert client.get(final["artifacts"][0]["download_url"]).text.startswith(candidate["content"])
        requested = [e["data"] for e in events if e["kind"] == "tool.requested" and e["data"]["tool"] == "task_finish"]
        assert [e["arguments"] for e in requested] == [{"artifact_ids": chosen}]
        dispatched = next(e["data"] for e in events if e["kind"] == "tool.dispatched" and e["data"]["tool"] == "task_finish")
        assert dispatched["scope"]["completion_receipts"] == [{"id": chosen[0], "name": "snapshot.md", "sha256": final["artifacts"][0]["sha256"]}]
        submitted = {"task": run["task"], "sources": ["facts.md"], "completion_workflow": "artifact_receipt",
                     "publication_workflow": "draft_review", "require_publish_approval": True, "submission_key": "receipt-submit"}
        endpoint = f"/api/sessions/{run['session_id']}/runs"
        assert client.post(endpoint, json=submitted).json()["id"] == run["id"]
        assert client.post(endpoint, json={**submitted, "completion_workflow": "model_summary"}).status_code == 409
        assert len(peer.requests) == 5


@pytest.mark.parametrize("bad", ["unknown", "empty", "duplicate", "extra_summary", "text_reply", "legacy_call"])
def test_receipt_rejects_unconfirmed_or_freeform_completion_before_dispatch(tmp_path: Path, bad: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps": ["Read and save"]}),
                           json_reply("source_read", {"path": "facts.md", "labels": []}),
                           json_reply("artifact_write", candidate), json_reply("task_finish", {})])
    async def invalid_receipt(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 3:
            body = json.loads(request.content)
            identity = json.loads(body["messages"][-1]["content"])["tool_result"]["value"]["id"]
            args: dict = {"artifact_ids": [identity]}
            if bad == "unknown": args["artifact_ids"] = ["f" * 32]
            if bad == "empty": args["artifact_ids"] = []
            if bad == "duplicate": args["artifact_ids"] = [identity, identity]
            if bad == "extra_summary": args["summary"] = "Anomaly rate calculated."
            value: dict = {"reply": args}
            if bad == "text_reply": value = {"reply": "Anomaly rate calculated."}
            if bad == "legacy_call": value = {"calls": [{"name": "task_finish", "arguments": {"summary": "Anomaly rate calculated."}}]}
            peer.replies[3] = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(value)}}]})
        return await peer(request)
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(invalid_receipt))) as client:
        final, events = finish(client, create_task(client, source, completion_workflow="artifact_receipt",
                                                  limits={"max_corrections": 0}))
        assert final["status"] == "failed" and final["result"] == "", final
        assert len(final["artifacts"]) == 1
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "task_finish" for e in events)
        assert not any(e["kind"] == "run.completed" for e in events)


def test_real_restart_retains_receipt_policy_and_confirms_inherited_publication(tmp_path: Path) -> None:
    from draft_process import running_draft_service, wait_draft_tool, finish_draft_run
    from test_recovery import create_process_task, stop
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    replies = [json_reply("plan_update", {"steps": ["Read and publish"]}),
               json_reply("source_read", {"path": "facts.md", "labels": []}), json_reply("artifact_write", candidate)]
    store = tmp_path / "store"
    with running_draft_service(store, replies, decision_protocol="json_tools") as first:
        original = create_process_task(first.client, source, completion_workflow="artifact_receipt")
        wait_draft_tool(first.client, original["id"], "tool.completed", "artifact_write")
        published = first.client.get(f"/api/runs/{original['id']}").json()["artifacts"][0]
        stop(first.process)
    reply = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"reply": {"artifact_ids": [published["id"]]}})}}]})
    with running_draft_service(store, [reply], decision_protocol="json_tools") as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        assert checkpoint["policy"]["completion_workflow"] == "artifact_receipt"
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={
            "checkpoint_sha256": checkpoint["sha256"], "submission_key": "receipt-resume"})
        assert response.status_code == 202, response.text
        final, events = finish_draft_run(second.client, response.json()["id"])
        assert final["status"] == "completed", final["error"]
        assert final["result"] == "已發布：snapshot.md。"
        assert final["artifacts"] == [] and final["inherited_artifacts"][0]["id"] == published["id"]
        dispatched = [e["data"]["tool"] for e in events if e["kind"] == "tool.dispatched"]
        assert dispatched == ["task_finish"]
        requests = json.loads(second.model_requests.read_text(encoding="utf-8"))
        assert len(requests) == 1 and published["id"] in json.dumps(requests[0])


@pytest.mark.parametrize("blocker,code", [("draft", "unpublished_draft"), ("pending", "unresolved_tool_error"),
                                         ("batch", "invalid_completion"), ("plain", "receipt_required")])
def test_receipt_preserves_completion_gates(tmp_path: Path, blocker: str, code: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    replies = [tool_reply("plan_update", {"steps": ["Read and save"]}, "plan"),
               tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
               tool_reply("artifact_draft", candidate, "draft"), tool_reply("artifact_write", candidate, "write")]
    if blocker == "draft": replies.append(tool_reply("artifact_draft", candidate, "newdraft"))
    if blocker == "pending": replies.append(tool_reply("source_read", {"path": "facts.md", "labels": 42}, "badread"))
    finish_index = len(replies)
    peer = CompletionPeer(replies + [tool_reply("task_finish", {}, "finish")])
    async def premature_finish(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == finish_index:
            body = json.loads(request.content)
            published = next(json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"
                             and "download_url" in json.loads(m["content"]))
            response = tool_reply("task_finish", {"artifact_ids": [published["id"]]}, "finish")
            if blocker == "batch":
                value = response.json()
                value["choices"][0]["message"]["tool_calls"].extend(tool_reply("plan_update", {"steps": ["Done"]}, "extra").json()["choices"][0]["message"]["tool_calls"])
                response = httpx.Response(200, json=value)
            if blocker == "plain": response = httpx.Response(200, json={"choices": [{"message": {"content": "Anomaly rate calculated."}}]})
            peer.replies[finish_index] = response
        return await peer(request)
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(premature_finish))) as client:
        final, events = finish(client, create_task(client, source, completion_workflow="artifact_receipt", publication_workflow="draft_review"))
        assert final["status"] == "failed" and final["error"]["code"] == code, final
        assert len(final["artifacts"]) == 1
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "task_finish" for e in events)


def test_receipt_cannot_select_a_real_artifact_from_an_unrelated_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "other.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    def publication(name: str) -> list[httpx.Response]:
        return [json_reply("plan_update", {"steps": ["Read and publish"]}),
                json_reply("source_read", {"path": "facts.md", "labels": []}), json_reply("artifact_write", {**candidate, "name": name})]
    first = CompletionPeer(publication("other.md") + [json_reply("task_finish", {"summary": "Saved."})])
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(first))) as client:
        original, _ = finish(client, create_task(client, source))
        foreign = original["artifacts"][0]["id"]
    wire = json.dumps({"reply": {"artifact_ids": [foreign]}})
    second = CompletionPeer(publication("current.md") + [httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": wire}}]})])
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(second))) as client:
        final, events = finish(client, create_task(client, source, completion_workflow="artifact_receipt"))
        assert final["status"] == "failed" and final["error"]["code"] == "invalid_completion", final
        assert final["artifacts"][0]["id"] != foreign
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "task_finish" for e in events)


@pytest.mark.parametrize("decision", ["deny", "cancel"])
def test_denied_or_cancelled_publication_never_produces_a_receipt(tmp_path: Path, decision: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps": ["Read and publish"]}),
                           json_reply("source_read", {"path": "facts.md", "labels": []}),
                           json_reply("artifact_write", candidate)])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source, completion_workflow="artifact_receipt", require_publish_approval=True)
        approval = pending_approval(client, run)
        if decision == "deny":
            response = client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={
                "decision": "deny", "parameters_sha256": approval["parameters_sha256"]})
        else:
            response = client.post(f"/api/runs/{run['id']}/cancel")
        assert response.is_success
        final, events = finish(client, run)
        assert final["status"] == ("failed" if decision == "deny" else "cancelled")
        assert final["artifacts"] == [] and len(peer.requests) == 3
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "task_finish" for e in events)


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_streamed_receipt_is_atomic_and_truncation_cannot_complete(tmp_path: Path, finish_reason: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([streamed_json_reply("plan_update", {"steps": ["Read and publish"]}),
                           streamed_json_reply("source_read", {"path": "facts.md", "labels": []}),
                           streamed_json_reply("artifact_write", candidate), json_reply("task_finish", {})])
    async def streamed_receipt(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 3:
            body = json.loads(request.content)
            identity = json.loads(body["messages"][-1]["content"])["tool_result"]["value"]["id"]
            wire = json.dumps({"reply": {"artifact_ids": [identity]}})
            half = len(wire) // 2
            frames = [{"choices": [{"delta": {"content": wire[:half]}}]},
                      {"choices": [{"delta": {"content": wire[half:]}}]},
                      {"choices": [{"delta": {}, "finish_reason": finish_reason}]}]
            content = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
            peer.replies[3] = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())
        return await peer(request)
    settings = ModelSettings.local("contract-model", decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(streamed_receipt))) as client:
        final, events = finish(client, create_task(client, source, completion_workflow="artifact_receipt"))
        assert len(final["artifacts"]) == 1
        assert not any(e["kind"] == "model.text_delta" for e in events)
        if finish_reason == "stop":
            assert final["status"] == "completed" and final["result"] == "已發布：snapshot.md。"
        else:
            assert final["status"] == "failed" and final["error"]["code"] == "model_output_truncated"
            assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "task_finish" for e in events)
