from pathlib import Path
import asyncio
import json
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_approval import pending_approval
from test_local_model import LocalPeer, qwen_thinking_metadata
from test_workbench import create_task, finish, tool_reply


class CompletionPeer(LocalPeer):
    def __init__(self, replies: list[httpx.Response]) -> None:
        super().__init__()
        self.replies = replies
        self.requests: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        if request.url.path != "/v1/chat/completions":
            return await super().__call__(request)
        self.requests.append(json.loads(request.content))
        assert len(self.requests) <= len(self.replies), "No extra generation after the declared terminal response."
        return self.replies[len(self.requests)-1]


def publication_replies() -> list[httpx.Response]:
    return [
        tool_reply("plan_update", {"steps": ["Read, publish and finish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "write"),
    ]


def test_planning_catalog_explains_when_source_tools_become_available(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Saved."}, "finish")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, _ = finish(client, create_task(client, source))
        assert final["status"] == "completed", final
    first, second = peer.requests[:2]
    assert [tool["function"]["name"] for tool in first["tools"]] == ["plan_update"]
    explanation = "目前僅開放規劃；提交 plan_update 後會提供來源與發布工具，不能據此判斷沒有來源。"
    assert explanation in first["messages"][0]["content"]
    assert explanation not in second["messages"][0]["content"]
    assert {"source_read", "workspace_list", "artifact_write"} <= {tool["function"]["name"] for tool in second["tools"]}


def test_explicit_completion_follows_approved_publication_without_another_model_turn(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "來源快照已保存。"}, "finish")])
    requests = peer.requests
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source, require_publish_approval=True)
        approval = pending_approval(client, run)
        assert len(requests) == 3
        assert all("task_finish" not in {tool["function"]["name"] for tool in body["tools"]} for body in requests)
        assert all("Published." not in body["messages"][0]["content"] for body in requests)
        assert not client.get(f"/api/runs/{run['id']}").json()["artifacts"]
        response = client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}", json={
            "decision": "approve", "parameters_sha256": approval["parameters_sha256"],
        })
        assert response.status_code == 200, response.text
        final, events = finish(client, run)
        assert final["status"] == "completed", final["error"]
        assert final["result"] == "來源快照已保存。"
        assert final["usage"] == {"model_calls": 4, "tool_calls": 4}
        assert len(requests) == 4
        assert {"task_finish", "source_read", "artifact_write"} <= {tool["function"]["name"] for tool in requests[-1]["tools"]}
        assert "Published." in requests[-1]["messages"][0]["content"]
        assert final["approvals"][0]["status"] == "consumed"
        assert len(final["artifacts"]) == 1
        acknowledgement = requests[-1]["messages"][-1]
        assert acknowledgement["role"] == "tool"
        saved_result = json.loads(acknowledgement["content"])
        assert saved_result["id"] == final["artifacts"][0]["id"]
        assert saved_result["sha256"] == final["artifacts"][0]["sha256"]
        published = next(event["sequence"] for event in events if event["kind"] == "artifact.published")
        finished = next(event["sequence"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "task_finish")
        assert published < finished < events[-1]["sequence"]
        assert events[-1]["kind"] == "run.completed"


def test_completion_cannot_replace_a_missing_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer([tool_reply("task_finish", {"summary": "Done."}, "premature")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed" and run["error"]["code"] == "missing_artifact"
        assert run["usage"]["tool_calls"] == 1
        assert not run["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" for event in events)


def test_completion_cannot_be_preplanned_in_the_publication_decision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    replies = publication_replies()
    batch = replies.pop().json()
    batch["choices"][0]["message"]["tool_calls"] += tool_reply("task_finish", {"summary": "Done."}, "premature").json()["choices"][0]["message"]["tool_calls"]
    peer = CompletionPeer(replies + [httpx.Response(200, json=batch)])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed" and run["error"]["code"] == "invalid_completion"
        assert len(run["artifacts"]) == 1
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "task_finish" for event in events)
        assert not any(event["kind"] == "run.completed" for event in events)


def test_empty_stop_after_publication_gets_one_finish_only_recovery(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [httpx.Response(200, json={"choices": [{
        "message": {"content": None, "reasoning": "PRIVATE-MARKER"}, "finish_reason": "stop"}]}),
        tool_reply("task_finish", {"summary": "Saved snapshot.md."}, "recovered-finish")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "completed", run["error"]
        assert run["result"] == "Saved snapshot.md."
        assert run["usage"] == {"model_calls": 5, "tool_calls": 4}
        assert len(run["artifacts"]) == 1
        assert [tool["function"]["name"] for tool in peer.requests[-1]["tools"]] == ["task_finish"]
        assert "The previous completion reply contained no visible text." in peer.requests[-1]["messages"][0]["content"]
        assert peer.requests[-1]["messages"][-1]["role"] == "tool"
        recovery = [event for event in events if event["kind"] == "completion.recovery_requested"]
        assert len(recovery) == 1
        assert recovery[0]["data"] == {"failed_step": 4, "retry_step": 5, "reason": "empty_response",
                                        "remaining_corrections": 1, "tools": ["task_finish"]}
        assert next(event for event in events if event["kind"] == "model.requested" and event["data"]["step"] == 5)["data"]["completion_recovery"] is True
        assert "PRIVATE-MARKER" not in json.dumps({"run": run, "events": events})
        assert events[-1]["kind"] == "run.completed"


def test_a_second_empty_completion_response_still_fails_without_more_retries(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    empty = lambda: httpx.Response(200, json={"choices": [{"message": {"content": None, "reasoning": "PRIVATE-MARKER"},
                                                            "finish_reason": "stop"}]})
    peer = CompletionPeer(publication_replies() + [empty(), empty()])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed" and run["error"]["diagnostics"]["reason"] == "empty_response"
        assert len(peer.requests) == 5
        assert [tool["function"]["name"] for tool in peer.requests[-1]["tools"]] == ["task_finish"]
        assert len([event for event in events if event["kind"] == "completion.recovery_requested"]) == 1
        assert not any(event["kind"] == "run.completed" for event in events)
        assert "PRIVATE-MARKER" not in json.dumps({"run": run, "events": events})


def test_empty_completion_does_not_retry_without_correction_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [httpx.Response(200, json={"choices": [{
        "message": {"content": None, "reasoning": "PRIVATE-MARKER"}, "finish_reason": "stop"}]})])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, limits={"max_corrections": 0}))
        assert run["status"] == "failed" and run["error"]["diagnostics"]["reason"] == "empty_response"
        assert len(peer.requests) == 4
        assert not any(event["kind"] == "completion.recovery_requested" for event in events)
        assert not any(event["kind"] == "run.completed" for event in events)


def test_completion_obeys_the_existing_tool_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Done."}, "finish")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, limits={"max_tool_calls": 3}))
        assert run["status"] == "failed" and run["error"]["code"] == "tool_limit_exceeded"
        assert run["usage"]["tool_calls"] == 3
        assert len(run["artifacts"]) == 1
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "task_finish" for event in events)


def test_completion_requires_a_nonblank_public_summary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer([tool_reply("task_finish", {"summary": " \n\t"}, "empty")])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, limits={"max_corrections": 0}))
        assert run["status"] == "failed" and run["error"]["code"] == "invalid_arguments"
        assert not any(event["kind"] == "tool.dispatched" for event in events)


def test_completion_can_correct_its_own_summary_after_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [
        tool_reply("task_finish", {"summary": " "}, "bad-finish"),
        tool_reply("task_finish", {"summary": "Snapshot saved."}, "fixed-finish"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "completed", run["error"]
        assert run["result"] == "Snapshot saved."
        assert run["usage"] == {"model_calls": 5, "tool_calls": 5}
        assert "task_finish" in {tool["function"]["name"] for tool in peer.requests[-1]["tools"]}
        corrected = next(event["data"] for event in events if event["kind"] == "correction.observed")
        assert corrected["failed_call_ids"] == ["bad-finish"]
        assert corrected["successful_call_id"] == "fixed-finish"
        assert corrected["failed_decision_steps"] == [4] and corrected["decision_step"] == 5


@pytest.mark.parametrize("summary,expected", [("Original.", "completed"), ("Changed.", "failed")])
def test_completion_correction_preserves_fields_outside_the_allowed_edit(tmp_path: Path, summary: str, expected: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = CompletionPeer(publication_replies() + [
        tool_reply("task_finish", {"summary": "Original.", "extra": True}, "bad-finish"),
        tool_reply("task_finish", {"summary": summary}, "fixed-finish"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == expected, run["error"]
        feedback = next(event["data"] for event in events if event["kind"] == "correction.available")
        assert feedback["error"]["correctable_fields"] == ["extra"]
        if expected == "failed":
            assert run["error"]["code"] == "unresolved_tool_error"
            assert not any(event["kind"] == "correction.observed" for event in events)
            assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "task_finish" for event in events)
        else:
            assert run["result"] == "Original."
            assert any(event["kind"] == "correction.observed" for event in events)


def test_completion_cannot_clear_an_unresolved_read_error(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    (source / "extra.md").write_bytes(b"Other fact.\n")
    replies = publication_replies()[:2] + [
        tool_reply("source_read", {"path": "extra.md", "labels": []}, "other"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": "Other fact. [extra.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Done."}, "finish"),
    ]
    peer = CompletionPeer(replies)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer), fault_once_source_read=True)) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md", "extra.md"]))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error"
        assert len(run["artifacts"]) == 1
        assert "task_finish" not in {tool["function"]["name"] for tool in peer.requests[-1]["tools"]}
        assert not any(event["kind"] == "correction.observed" for event in events)
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "task_finish" for event in events)


def test_cancel_while_waiting_for_completion_cannot_become_completed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    ready, release, stopped = Event(), Event(), Event()
    scripted = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "Done."}, "finish")])
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(scripted.requests) == 3:
            ready.set()
            try:
                assert await asyncio.to_thread(release.wait, 5)
            finally:
                stopped.set()
        return await scripted(request)
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source)
        try:
            assert ready.wait(5)
            assert len(client.get(f"/api/runs/{run['id']}").json()["artifacts"]) == 1
            assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 202
            final, events = finish(client, run)
            assert final["status"] == "cancelled" and len(final["artifacts"]) == 1
        finally:
            release.set()
        assert stopped.wait(5)
        assert client.get(f"/api/runs/{run['id']}/events").json()["events"] == events
        assert not any(event["kind"] == "run.completed" for event in events)
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "task_finish" for event in events)


def test_recorded_thinking_trace_fits_original_context_with_explicit_finish(tmp_path: Path) -> None:
    """Recorded tool decisions plus a synthetic finish; not real-model quality."""
    root = Path(__file__).resolve().parents[1]
    recorded = root / "tests/fixtures/recorded/batch-27/analysis-standard"
    original = json.loads((recorded / "run.json").read_text(encoding="utf-8"))
    saved_events = json.loads((recorded / "events.json").read_text(encoding="utf-8"))
    replies = [httpx.Response(200, json={"choices": [{"message": event["data"]["message"]}]})
               for event in saved_events if event["kind"] == "model.responded"]
    peer = CompletionPeer(replies + [tool_reply("task_finish", {"summary": "Report ready."}, "finish")])
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-standard/metrics.csv").read_bytes())
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, task=original["task"], sources=["metrics.csv"]))
        assert run["status"] == "completed", run["error"]
        assert run["usage"] == {"model_calls": 7, "tool_calls": 8}
        assert run["artifacts"][0]["sha256"] == "a520488201129d0bc5f3af413ee8b2fac7f2ff70128d435fac93c568130bc464"
        budgets = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"]
        assert all(budget["upper_bound_tokens"] <= 8192 and budget["output_token_reserve"] == 1024 for budget in budgets)
        assert all(any(message.get("content") == original["task"] for message in body["messages"]) for body in peer.requests)
        assert peer.requests[-1]["messages"][0]["content"].startswith(peer.requests[1]["messages"][0]["content"])
        assert json.loads(peer.requests[-1]["messages"][-1]["content"])["sha256"] == run["artifacts"][0]["sha256"]
