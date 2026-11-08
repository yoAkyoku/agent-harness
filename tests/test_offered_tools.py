"""A tool call is executed only if that tool was offered in the request that produced it.

The json_tools protocol already rejects unknown names while decoding; native tool calls
are checked just before dispatch, so both protocols honour the same catalogue."""
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.runtime import request_digest
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import create_task, finish, tool_reply


def offered(request: dict) -> set[str]:
    return {tool["function"]["name"] for tool in request["tools"]}


def test_a_tool_that_was_not_offered_is_rejected_before_it_runs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Revenue was 42.\n", encoding="utf-8")
    peer = CompletionPeer([
        # Only plan_update is offered before a plan exists; an early call must not execute,
        # and since the model never needs that tool again it must not block completion.
        tool_reply("workspace_list", {}, "early-read"),
        tool_reply("plan_update", {"steps": ["Read", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved snapshot.md with revenue 42."}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)

    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))

    assert run["status"] == "completed", run["error"]
    assert offered(peer.requests[0]) == {"plan_update"}
    rejected = [event for event in events if event["kind"] == "tool.rejected"]
    assert [(event["data"]["call_id"], event["data"]["code"]) for event in rejected] == [
        ("early-read", "tool_not_offered")]
    executed = [event["data"]["call_id"] for event in events if event["kind"] == "tool.dispatched"]
    assert "early-read" not in executed
    feedback = next(message for message in peer.requests[1]["messages"]
                    if message["role"] == "tool" and message.get("tool_call_id") == "early-read")
    assert '"tool_not_offered"' in feedback["content"] and '"retryable": true' in feedback["content"]
    requested = [event["data"] for event in events if event["kind"] == "model.requested"]
    assert requested[0]["offered_tools"] == ["plan_update"]
    # The recorded digest identifies the exact body the provider received.
    assert [data["request_sha256"] for data in requested] == [request_digest(body) for body in peer.requests]
