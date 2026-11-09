"""Permission rules for MCP tools: allow (read-only), ask (each call needs approval), deny (everything else).

A tool in `askTools` may have side effects; every call waits for a single-use approval bound
to the exact arguments and scope, and a denied or missing approval means it never runs.
"""
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_approval import pending_approval
from test_completion import CompletionPeer
from test_mcp_tools import SERVER, _start
from test_workbench import finish, tool_reply

CREATE = "mcp__notes__create_note"
SEARCH = "mcp__notes__search_notes"


def _config(tmp_path: Path, **lists: list[str]) -> Path:
    path = tmp_path / "mcp.json"
    entry = {"command": sys.executable, "args": [str(SERVER)],
             "allowedTools": lists.get("allowed", ["search_notes"]),
             "askTools": lists.get("ask", ["create_note", "delete_note"])}
    path.write_text(json.dumps({"mcpServers": {"notes": entry}}), encoding="utf-8")
    return path


def _app(tmp_path: Path, replies: list[httpx.Response]) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer(replies)),
                                 mcp_config=_config(tmp_path)))


def _replies() -> list[httpx.Response]:
    return [
        tool_reply("plan_update", {"steps": ["Record the owner", "Check it", "Publish"]}, "plan"),
        tool_reply(CREATE, {"name": "retro", "text": "Action owner: Leo"}, "create"),
        tool_reply(SEARCH, {"query": "Leo"}, "search"),
        tool_reply("artifact_write", {"name": "owner.md", "content": "Action owner: Leo [mcp/notes/search_notes E2]",
                                      "citations": ["E2"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved owner.md: Leo owns the action."}, "finish"),
    ]


def test_ask_tools_are_offered_with_an_approval_requirement(tmp_path: Path) -> None:
    with _app(tmp_path, []) as client:
        notes = client.get("/api/config").json()["mcp"]["servers"]["notes"]
    approval = {tool["name"]: tool["approval"] for tool in notes["tools"]}
    assert approval == {CREATE: "required", "mcp__notes__delete_note": "required", SEARCH: "none"}
    assert {item["tool"]: item["reason"] for item in notes["excluded"]} == {
        "env_has": "not_allowed", "lookup_note": "not_allowed", "word_count": "not_allowed"}


def test_a_tool_listed_as_both_allowed_and_ask_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both"):
        create_app(tmp_path / "store", mcp_config=_config(tmp_path, allowed=["create_note"], ask=["create_note"]))


def test_side_effect_call_runs_only_after_a_single_use_approval(tmp_path: Path) -> None:
    with _app(tmp_path, _replies()) as client:
        run = _start(client, tmp_path, mcp_tools=[CREATE, SEARCH])
        approval = pending_approval(client, run)
        assert approval["tool"] == CREATE
        assert approval["arguments"] == {"name": "retro", "text": "Action owner: Leo"}
        assert approval["scope"]["approval"] == "required" and approval["scope"]["effect"] == "external_effect"
        events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == CREATE for event in events)
        response = client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}",
                               json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]})
        assert response.status_code == 200, response.text
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    kinds = [event["kind"] for event in events]
    assert kinds.count("approval.consumed") == 1
    dispatched = next(event["data"] for event in events
                      if event["kind"] == "tool.dispatched" and event["data"]["tool"] == CREATE)
    assert dispatched["effect"] == "external_effect"
    searched = next(event["data"]["result"] for event in events
                    if event["kind"] == "tool.completed" and event["data"]["tool"] == SEARCH)
    assert "Action owner: Leo" in searched["text"]  # the approved call really ran, once


def test_denied_side_effect_call_never_runs(tmp_path: Path) -> None:
    with _app(tmp_path, _replies()) as client:
        run = _start(client, tmp_path, mcp_tools=[CREATE, SEARCH])
        approval = pending_approval(client, run)
        client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}",
                    json={"decision": "deny", "parameters_sha256": approval["parameters_sha256"]})
        final, events = finish(client, run)

    assert final["status"] == "failed" and final["error"]["code"] == "approval_denied"
    assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == CREATE for event in events)
