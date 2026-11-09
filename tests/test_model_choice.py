"""Each Run may choose among the models (and, for Claude, effort levels) the workbench was started with."""
import asyncio
import json
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from agent_harness.__main__ import build_settings, parser
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_remote_providers import REPORT, _message, _source, _tool_use
from test_workbench import finish, text_reply, tool_reply

DEFAULT, LARGER = "example-model", "example-large"


def _openai(monkeypatch: pytest.MonkeyPatch) -> ModelSettings:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-real")
    return ModelSettings.openai_compatible(DEFAULT, base_url="https://llm.example.test/v1",
                                           api_key_env="TEST_PROVIDER_KEY", alternatives=(LARGER,))


def _replies() -> list[httpx.Response]:
    return [
        tool_reply("plan_update", {"steps": ["Read the source", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Published snapshot.md with revenue 42."}, "finish"),
    ]


def _session(client: TestClient, tmp_path: Path) -> str:
    workspace = client.post("/api/workspaces", json={"path": str(_source(tmp_path))}).json()
    return client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()["id"]


def test_a_run_can_use_another_configured_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies, requests = _replies() + _replies(), []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return replies[len(requests) - 1]

    with TestClient(create_app(tmp_path / "store", model_settings=_openai(monkeypatch),
                               model_transport=httpx.MockTransport(provider))) as client:
        choices = client.get("/api/config").json()["model"]["choices"]
        session = _session(client, tmp_path)
        chosen = client.post(f"/api/sessions/{session}/runs", json={
            "task": "Save a source snapshot.", "sources": ["facts.md"], "model": LARGER}).json()
        chosen, _ = finish(client, chosen)
        default = client.post(f"/api/sessions/{session}/runs",
                              json={"task": "Save a source snapshot.", "sources": ["facts.md"]}).json()
        default, _ = finish(client, default)

    assert choices == {"models": [DEFAULT, LARGER], "efforts": []}
    assert chosen["status"] == default["status"] == "completed"
    assert chosen["model"]["name"] == LARGER and default["model"]["name"] == DEFAULT
    assert [body["model"] for body in requests] == [LARGER] * 4 + [DEFAULT] * 4


def test_unknown_models_and_unsupported_effort_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(create_app(tmp_path / "store", model_settings=_openai(monkeypatch),
                               model_transport=httpx.MockTransport(lambda request: httpx.Response(500)))) as client:
        session = _session(client, tmp_path)
        for extra, code in (({"model": "not-configured"}, "model_unavailable"), ({"effort": "high"}, "effort_unavailable")):
            response = client.post(f"/api/sessions/{session}/runs",
                                   json={"task": "Save.", "sources": ["facts.md"], **extra})
            assert response.status_code == 422, response.text
            assert response.json()["detail"]["code"] == code


def test_claude_run_can_choose_model_and_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-not-real")
    replies = [
        _message(_tool_use("toolu_plan", "plan_update", {"steps": ["Read the source", "Publish"]}), "tool_use"),
        _message(_tool_use("toolu_read", "source_read", {"path": "facts.md", "labels": []}), "tool_use"),
        _message(_tool_use("toolu_write", "artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}), "tool_use"),
        _message(_tool_use("toolu_finish", "task_finish", {"summary": "Published snapshot.md with revenue 42."}), "tool_use"),
    ]
    requests: list[dict] = []

    def provider(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return replies[len(requests) - 1]

    settings = ModelSettings.anthropic("claude-opus-5-5", api_key_env="TEST_ANTHROPIC_KEY", alternatives=("claude-sonnet-5-5",))
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               anthropic_transport=httpx2.MockTransport(provider))) as client:
        assert client.get("/api/config").json()["model"]["choices"] == {
            "models": ["claude-opus-5-5", "claude-sonnet-5-5"], "efforts": ["low", "medium", "high", "xhigh", "max"]}
        session = _session(client, tmp_path)
        run = client.post(f"/api/sessions/{session}/runs", json={
            "task": "Save a source snapshot.", "sources": ["facts.md"], "model": "claude-sonnet-5-5", "effort": "high"}).json()
        final, _ = finish(client, run)

    assert final["status"] == "completed", final["error"]
    assert final["model"]["name"] == "claude-sonnet-5-5" and final["model"]["effort"] == "high"
    assert {(body["model"], body["output_config"]["effort"]) for body in requests} == {("claude-sonnet-5-5", "high")}


def test_resume_keeps_the_runs_chosen_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _openai(monkeypatch)
    first_requests: list[dict] = []

    async def interrupted(request: httpx.Request) -> httpx.Response:
        first_requests.append(json.loads(request.content))
        if len(first_requests) == 1:
            return tool_reply("plan_update", {"steps": ["Read the source", "Publish"]}, "plan")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    store = tmp_path / "store"
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(interrupted))) as first:
        session = _session(first, tmp_path)
        run = first.post(f"/api/sessions/{session}/runs", json={
            "task": "Save a source snapshot.", "sources": ["facts.md"], "model": LARGER}).json()
        from test_recovery import wait_event
        wait_event(first, run["id"], "plan.updated")
    resumed_requests: list[dict] = []
    replies = _replies()[1:]

    async def resumed_provider(request: httpx.Request) -> httpx.Response:
        resumed_requests.append(json.loads(request.content))
        return replies[len(resumed_requests) - 1]

    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(resumed_provider))) as second:
        checkpoint = second.get(f"/api/runs/{run['id']}/checkpoint").json()
        response = second.post(f"/api/runs/{run['id']}/resume",
                               json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-model-01"})
        assert response.status_code == 202, response.text
        final, _ = finish(second, response.json())

    assert final["status"] == "completed", final["error"]
    assert final["model"]["name"] == LARGER
    assert {body["model"] for body in resumed_requests} == {LARGER}


def test_cli_alternative_models_apply_to_remote_providers_only() -> None:
    claude = build_settings(parser().parse_args(["--provider", "anthropic", "--alt-model", "claude-sonnet-5-5"]))
    assert claude.alternatives == ("claude-sonnet-5-5",)
    with pytest.raises(ValueError):
        build_settings(parser().parse_args(["--model", "qwen3.5:9b-q4_K_M", "--alt-model", "other"]))
