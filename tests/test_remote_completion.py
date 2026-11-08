"""Remote providers finish through the same grounded completion check as local models.

Claude and OpenAI-compatible models use native tool calling without the local thinking
profile. They are offered task_finish once something is published, and a plain-text
ending after publication is checked against the published artifact like a task_finish
summary: an unsupported reply becomes a correctable, task_finish-only step.
"""
import json
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_remote_providers import REPORT, _claude_app, _message, _source, _tool_use
from test_workbench import create_task, finish, text_reply, tool_reply

GROUNDED = "Published snapshot.md with revenue 42."
UNSUPPORTED = "Published snapshot.md with revenue 420."


def _tool_names(body: dict) -> list[str]:
    return [tool["function"]["name"] if "function" in tool else tool["name"] for tool in body.get("tools", [])]


def _openai_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endings: list[httpx.Response],
                **task: object) -> tuple[dict, list[dict], list[dict]]:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-real")
    replies = [
        tool_reply("plan_update", {"steps": ["Read the source", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}, "write"),
        *endings,
    ]
    requests: list[dict] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= len(replies), "No extra generation after the scripted ending."
        return replies[len(requests) - 1]

    settings = ModelSettings.openai_compatible("example-model", base_url="https://llm.example.test/v1",
                                               api_key_env="TEST_PROVIDER_KEY")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(provider))) as client:
        run, events = finish(client, create_task(client, _source(tmp_path), **task))
    return run, events, requests


def test_openai_compatible_model_is_offered_task_finish_after_publishing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, events, requests = _openai_run(tmp_path, monkeypatch, [tool_reply("task_finish", {"summary": GROUNDED}, "finish")])

    assert run["status"] == "completed", run["error"]
    assert run["result"] == GROUNDED
    assert "task_finish" not in _tool_names(requests[2])
    assert "task_finish" in _tool_names(requests[3])


def test_unsupported_plain_text_ending_becomes_a_task_finish_correction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, events, requests = _openai_run(tmp_path, monkeypatch, [
        text_reply(UNSUPPORTED), tool_reply("task_finish", {"summary": GROUNDED}, "finish")])

    assert run["status"] == "completed", run["error"]
    assert run["result"] == GROUNDED
    recovery = next(event["data"] for event in events if event["kind"] == "completion.recovery_requested")
    assert recovery["reason"] == "unsupported_completion_claim"
    assert recovery["error"]["code"] == "unsupported_completion_claim" and "420" in recovery["error"]["message"]
    retry = requests[4]
    assert _tool_names(retry) == ["task_finish"]
    assert "420" in retry["messages"][0]["content"]  # the rejection reason reaches the model
    assert not any(message.get("content") == UNSUPPORTED for message in retry["messages"])


def test_grounded_plain_text_ending_still_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, events, requests = _openai_run(tmp_path, monkeypatch, [text_reply(GROUNDED)])

    assert run["status"] == "completed", run["error"]
    assert run["result"] == GROUNDED
    assert not any(event["kind"] == "completion.recovery_requested" for event in events)


def test_unsupported_plain_text_ending_fails_without_a_correction_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, events, requests = _openai_run(tmp_path, monkeypatch, [text_reply(UNSUPPORTED)], limits={"max_corrections": 0})

    assert run["status"] == "failed"
    assert run["error"]["code"] == "unsupported_completion_claim"


def test_claude_unsupported_text_ending_is_corrected_through_task_finish(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-not-real")
    app, requests = _claude_app(tmp_path, [
        _message(_tool_use("toolu_plan", "plan_update", {"steps": ["Read the source", "Publish"]}), "tool_use"),
        _message(_tool_use("toolu_read", "source_read", {"path": "facts.md", "labels": []}), "tool_use"),
        _message(_tool_use("toolu_write", "artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}), "tool_use"),
        _message([{"type": "text", "text": UNSUPPORTED}], "end_turn"),
        _message(_tool_use("toolu_finish", "task_finish", {"summary": GROUNDED}), "tool_use"),
    ])
    with app as client:
        run, events = finish(client, create_task(client, _source(tmp_path)))

    assert run["status"] == "completed", run["error"]
    assert run["result"] == GROUNDED
    assert "task_finish" in _tool_names(requests[3])
    retry = requests[4]
    assert _tool_names(retry) == ["task_finish"]
    assert "420" in retry["system"]
    # The rejected reply is not left as a trailing assistant turn (which the Messages API would treat as a prefill).
    assert retry["messages"][-1]["role"] == "user"
    roles = [message["role"] for message in retry["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_rejected_plain_text_ending_is_not_replayed_in_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-real")
    replies = [
        tool_reply("plan_update", {"steps": ["Read the source", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}, "write"),
        text_reply(UNSUPPORTED),
        tool_reply("task_finish", {"summary": GROUNDED}, "finish"),
        tool_reply("plan_update", {"steps": ["Revise"]}, "revision-plan"),
        text_reply("No changes."),  # the revision itself does not matter here
    ]
    requests: list[dict] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return replies[min(len(requests), len(replies)) - 1]

    settings = ModelSettings.openai_compatible("example-model", base_url="https://llm.example.test/v1",
                                               api_key_env="TEST_PROVIDER_KEY")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(provider))) as client:
        run, _ = finish(client, create_task(client, _source(tmp_path)))
        assert run["status"] == "completed", run["error"]
        revision = client.post(f"/api/sessions/{run['session_id']}/runs", json={
            "task": "Revise it.", "sources": ["facts.md"],
            "base_artifact": {"run_id": run["id"], "artifact_id": run["artifacts"][0]["id"]},
        }).json()
        finish(client, revision)

    history = requests[5]["messages"]
    assert any(message.get("content") == GROUNDED or GROUNDED in str(message.get("content")) for message in history)
    assert not any(UNSUPPORTED in str(message.get("content")) for message in history)
