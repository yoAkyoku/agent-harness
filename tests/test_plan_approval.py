"""Plan approval: with `require_plan_approval`, nothing runs until the user approves the plan.

The plan is the Run's first decision. The user approves it (a single-use approval bound to
the exact steps) or asks for changes; a change request goes back to the model, together with
any message the user added, and the model must propose a plan the user accepts. A revised
plan needs a new approval; resubmitting the approved plan does not.
"""
import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_approval import pending_approval
from test_completion import CompletionPeer
from test_mcp_tools import _start
from test_recovery import wait_event
from test_workbench import finish, tool_reply

PLAN = ["Read facts.md", "Publish the revenue"]
REVISED = ["Read facts.md", "Check the revenue total", "Publish the revenue"]
WRITE = {"name": "revenue.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}


def _app(tmp_path: Path, replies: list[httpx.Response]) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(peer))), peer


def _next_approval(client: TestClient, run: dict, decided: list[dict]) -> dict:
    """The first approval request this test has not decided yet (the stream replays from the start)."""
    seen = {approval["id"] for approval in decided}
    with client.websocket_connect(f"/api/runs/{run['id']}/stream") as stream:
        while True:
            event = stream.receive_json()
            if event["kind"] == "approval.requested" and event["data"]["id"] not in seen:
                return event["data"]
            assert event["kind"] not in {"run.failed", "run.completed"}, (event["kind"], event["data"])


def _decide(client: TestClient, run: dict, approval: dict, decision: str) -> None:
    response = client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}",
                           json={"decision": decision, "parameters_sha256": approval["parameters_sha256"]})
    assert response.status_code == 200, response.text


def _tool_results(request: dict) -> list[dict]:
    return [json.loads(message["content"]) for message in request["messages"] if message["role"] == "tool"]


def test_nothing_runs_until_the_plan_is_approved(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": PLAN}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, require_plan_approval=True)
        assert run["policy"]["require_plan_approval"] is True
        approval = pending_approval(client, run)
        assert approval["tool"] == "plan_update" and approval["arguments"] == {"steps": PLAN}
        assert approval["scope"]["approval"] == "required" and approval["scope"]["effect"] == "run_metadata"
        assert client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_approval"
        assert len(peer.requests) == 1  # the model is not asked again while the plan waits
        events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert not any(event["kind"] in {"tool.dispatched", "plan.updated"} for event in events)
        _decide(client, run, approval, "approve")
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    kinds = [event["kind"] for event in events]
    assert kinds.count("approval.requested") == 1 and kinds.count("approval.consumed") == 1
    assert kinds.index("approval.consumed") < kinds.index("plan.updated")


def test_requested_changes_go_back_to_the_model_with_the_users_note(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": PLAN}, "plan"),
        tool_reply("plan_update", {"steps": REVISED}, "plan-2"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, require_plan_approval=True)
        first = pending_approval(client, run)
        sent = client.post(f"/api/runs/{run['id']}/messages", json={"message_id": "note-1", "text": "Also check the total."})
        assert sent.status_code == 202, sent.text
        _decide(client, run, first, "deny")
        second = _next_approval(client, run, [first])
        assert second["id"] != first["id"] and second["arguments"] == {"steps": REVISED}
        _decide(client, run, second, "approve")
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    revision_request = peer.requests[1]
    error = _tool_results(revision_request)[-1]["error"]
    assert error["code"] == "plan_changes_requested" and error["retryable"] is True
    assert any("Also check the total." in str(message.get("content")) for message in revision_request["messages"]
               if message["role"] == "user")
    # Only plan_update is offered until a plan is approved.
    assert [tool["function"]["name"] for tool in revision_request["tools"]] == ["plan_update"]
    # A change request is the user's decision, not a model error: it spends no correction budget.
    assert "remaining_corrections" not in error
    plans = [event["data"]["steps"] for event in events if event["kind"] == "plan.updated"]
    assert plans == [REVISED]
    assert not any(event["kind"] == "run.failed" for event in events)


def test_a_revised_plan_needs_a_new_approval_but_the_approved_plan_does_not(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, [
        tool_reply("plan_update", {"steps": PLAN}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("plan_update", {"steps": PLAN}, "same-plan"),
        tool_reply("plan_update", {"steps": REVISED}, "revised-plan"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, require_plan_approval=True)
        plan = pending_approval(client, run)
        _decide(client, run, plan, "approve")
        revision = _next_approval(client, run, [plan])
        assert revision["arguments"] == {"steps": REVISED}
        _decide(client, run, revision, "approve")
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    approvals = [event["data"]["call_id"] for event in events if event["kind"] == "approval.requested"]
    assert approvals == ["plan", "revised-plan"]
    plans = [event["data"]["steps"] for event in events if event["kind"] == "plan.updated"]
    assert plans == [PLAN, PLAN, REVISED]


def test_a_declined_revision_must_be_replaced_before_the_run_can_finish(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": PLAN}, "plan"),
        tool_reply("plan_update", {"steps": REVISED}, "revised-plan"),
        tool_reply("plan_update", {"steps": PLAN}, "back-to-approved"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, require_plan_approval=True)
        plan = pending_approval(client, run)
        _decide(client, run, plan, "approve")
        _decide(client, run, _next_approval(client, run, [plan]), "deny")
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    error = _tool_results(peer.requests[2])[-1]["error"]
    assert error["code"] == "plan_changes_requested"
    assert "last approved plan" in error["message"]
    kinds = [event["kind"] for event in events]
    assert kinds.count("approval.requested") == 2  # going back to the approved plan needs no new approval
    observed = next(event["data"] for event in events if event["kind"] == "correction.observed")
    assert observed["failed_call_ids"] == ["revised-plan"] and observed["successful_call_id"] == "back-to-approved"


def test_without_the_setting_plans_need_no_approval(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, [
        tool_reply("plan_update", {"steps": PLAN}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path)
        assert "require_plan_approval" not in run["policy"]
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    assert not any(event["kind"] == "approval.requested" for event in events)
    dispatched = next(event["data"] for event in events
                      if event["kind"] == "tool.dispatched" and event["data"]["tool"] == "plan_update")
    assert "approval" not in dispatched["scope"]


class _InterruptedPeer(CompletionPeer):
    """Answers the scripted replies, then keeps the next model request open until the app stops."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(self.requests) >= len(self.replies):
            await asyncio.Event().wait()
        return await super().__call__(request)


def test_a_resumed_run_keeps_the_approved_plan(tmp_path: Path) -> None:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    store = tmp_path / "store"
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(
            _InterruptedPeer([tool_reply("plan_update", {"steps": PLAN}, "plan")])))) as first:
        run = _start(first, tmp_path, require_plan_approval=True)
        _decide(first, run, pending_approval(first, run), "approve")
        wait_event(first, run["id"], "plan.updated")
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": PLAN}, "plan-again"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ])
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(peer))) as second:
        checkpoint = second.get(f"/api/runs/{run['id']}/checkpoint").json()
        response = second.post(f"/api/runs/{run['id']}/resume",
                               json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-plan-01"})
        assert response.status_code == 202, response.text
        resumed = response.json()
        assert resumed["policy"]["require_plan_approval"] is True
        final, events = finish(second, resumed)

    assert final["status"] == "completed", final["error"]
    assert not any(event["kind"] == "approval.requested" for event in events)


def test_the_bundled_fixture_proposes_its_plan_again_after_a_change_request(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        config = client.get("/api/config").json()
        workspace = client.post("/api/workspaces", json={"path": config["demo_path"]}).json()
        sources = client.get(f"/api/workspaces/{workspace['id']}/sources").json()["sources"]
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={
            "task": "Save selected sources.", "sources": [sources[0]["path"]], "require_plan_approval": True}).json()
        first = pending_approval(client, run)
        _decide(client, run, first, "deny")
        second = _next_approval(client, run, [first])
        assert second["tool"] == "plan_update" and second["call_id"] != first["call_id"]
        _decide(client, run, second, "approve")
        final, _ = finish(client, run)

    assert final["status"] == "completed", final["error"]
    assert len(final["artifacts"]) == 1
