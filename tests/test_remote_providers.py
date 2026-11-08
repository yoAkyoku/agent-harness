"""Remote model providers at the public HTTP seam: OpenAI-compatible endpoints and Claude.

The harness owns the loop for every provider; these tests only replace the
provider's HTTP transport and check the serialized requests the harness sends.
"""
import json
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_workbench import create_task, finish, tool_reply

REPORT = "Revenue was 42. [facts.md E1]"


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    return source


# ---------------------------------------------------------------- OpenAI-compatible

def test_openai_compatible_endpoint_runs_the_same_harness_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-real")
    replies = [
        tool_reply("plan_update", {"steps": ["Read the source", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}, "write"),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Published snapshot.md."}}]}),
    ]
    requests: list[httpx.Request] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return replies[len(requests) - 1]

    settings = ModelSettings.openai_compatible("example-model", base_url="https://llm.example.test/v1",
                                               api_key_env="TEST_PROVIDER_KEY")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(provider))) as client:
        config = client.get("/api/config").json()
        run, events = finish(client, create_task(client, _source(tmp_path)))
        assert run["status"] == "completed", run["error"]
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(REPORT)

    assert config["mode"] == "openai"
    assert config["model"]["context_profile"] == "remote_utf8_byte_upper_bound"
    assert all(str(r.url) == "https://llm.example.test/v1/chat/completions" for r in requests)
    assert all(r.headers["authorization"] == "Bearer test-key-not-real" for r in requests)
    body = json.loads(requests[0].content)
    assert body["model"] == "example-model" and body["max_tokens"] == 1024
    assert "temperature" not in body and "reasoning_effort" not in body
    assert run["result"] == "Published snapshot.md."
    # Real-model runs get the same citation requirement and verifiers as local models.
    writer = next(t for t in json.loads(requests[2].content)["tools"] if t["function"]["name"] == "artifact_write")
    assert "citations" in writer["function"]["parameters"]["required"]


def test_openai_compatible_endpoint_requires_its_api_key_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    settings = ModelSettings.openai_compatible("example-model", base_url="https://llm.example.test/v1",
                                               api_key_env="MISSING_PROVIDER_KEY")
    with pytest.raises(Exception, match="MISSING_PROVIDER_KEY"):
        with TestClient(create_app(tmp_path / "store", model_settings=settings,
                                   model_transport=httpx.MockTransport(lambda request: httpx.Response(500)))):
            pass


def test_openai_compatible_settings_reject_unsafe_base_urls() -> None:
    for url in ("http://llm.example.test/v1", "https://user:pass@llm.example.test/v1", "https://llm.example.test/v1?key=x"):
        with pytest.raises(ValueError):
            ModelSettings.openai_compatible("example-model", base_url=url)
    # Plain HTTP is allowed only for a loopback server (for example vLLM or LM Studio).
    assert ModelSettings.openai_compatible("local-model", base_url="http://127.0.0.1:8000/v1").base_url


# ---------------------------------------------------------------- Claude (Messages API)

def _message(content: list[dict], stop_reason: str) -> httpx2.Response:
    return httpx2.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    })


def _tool_use(identity: str, name: str, arguments: dict) -> list[dict]:
    # Current Claude models always think; the harness must not replay these blocks.
    return [{"type": "thinking", "thinking": "", "signature": "sig-" + identity},
            {"type": "tool_use", "id": identity, "name": name, "input": arguments}]


def _claude_app(tmp_path: Path, replies: list[httpx2.Response]) -> tuple[TestClient, list[dict]]:
    requests: list[dict] = []

    def provider(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-key-not-real"
        requests.append(json.loads(request.content))
        return replies[len(requests) - 1]

    settings = ModelSettings.anthropic("claude-opus-5-5", api_key_env="TEST_ANTHROPIC_KEY")
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 anthropic_transport=httpx2.MockTransport(provider))), requests


def test_claude_runs_the_harness_loop_with_native_tool_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-not-real")
    app, requests = _claude_app(tmp_path, [
        _message(_tool_use("toolu_plan", "plan_update", {"steps": ["Read the source", "Publish"]}), "tool_use"),
        _message(_tool_use("toolu_read", "source_read", {"path": "facts.md", "labels": []}), "tool_use"),
        _message(_tool_use("toolu_write", "artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}), "tool_use"),
        _message([{"type": "thinking", "thinking": "", "signature": "sig-end"},
                  {"type": "text", "text": "Published snapshot.md with revenue 42."}], "end_turn"),
    ])
    with app as client:
        config = client.get("/api/config").json()
        run, events = finish(client, create_task(client, _source(tmp_path)))
        assert run["status"] == "completed", run["error"]
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(REPORT)
    assert config["mode"] == "anthropic" and config["model"]["name"] == "claude-opus-5-5"
    assert run["result"] == "Published snapshot.md with revenue 42."

    first = requests[0]
    assert first["model"] == "claude-opus-5-5" and first["max_tokens"] == 1024
    assert isinstance(first["system"], str) and first["system"]
    assert first["messages"][0] == {"role": "user", "content": "Save a source snapshot."}
    assert "thinking" not in first  # always-on thinking is the model default
    assert first["output_config"] == {"effort": "low"}
    assert all(set(tool) == {"name", "description", "input_schema"} for tool in first["tools"])

    after_read = requests[2]["messages"]
    assert all(block.get("type") != "thinking"
               for message in after_read if isinstance(message["content"], list) for block in message["content"])
    assistant = next(m for m in reversed(after_read) if m["role"] == "assistant")
    assert assistant["content"][-1] == {"type": "tool_use", "id": "toolu_read", "name": "source_read",
                                        "input": {"path": "facts.md", "labels": []}}
    result = after_read[-1]
    assert result["role"] == "user" and result["content"][0]["type"] == "tool_result"
    assert result["content"][0]["tool_use_id"] == "toolu_read"
    assert "Revenue was 42." in result["content"][0]["content"]
    # Roles alternate after merging, as the Messages API expects.
    roles = [m["role"] for m in after_read]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_claude_tool_errors_are_returned_as_error_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-not-real")
    app, requests = _claude_app(tmp_path, [
        _message(_tool_use("toolu_plan", "plan_update", {"steps": ["Read the source", "Publish"]}), "tool_use"),
        _message(_tool_use("toolu_bad", "source_read", {"path": "facts.md", "labels": [], "offset": 3}), "tool_use"),
        _message(_tool_use("toolu_read", "source_read", {"path": "facts.md", "labels": []}), "tool_use"),
        _message(_tool_use("toolu_write", "artifact_write", {"name": "snapshot.md", "content": REPORT, "citations": ["E1"]}), "tool_use"),
        _message([{"type": "text", "text": "Published snapshot.md."}], "end_turn"),
    ])
    with app as client:
        run, events = finish(client, create_task(client, _source(tmp_path)))
        assert run["status"] == "completed", run["error"]
    error_result = requests[2]["messages"][-1]["content"][0]
    assert error_result["tool_use_id"] == "toolu_bad" and error_result["is_error"] is True
    assert any(e["kind"] == "correction.observed" for e in events)


@pytest.mark.parametrize("stop_reason,code", [("max_tokens", "model_output_truncated"), ("refusal", "model_refused")])
def test_claude_stop_reasons_fail_the_run_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     stop_reason: str, code: str) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-not-real")
    app, _ = _claude_app(tmp_path, [_message([{"type": "text", "text": "partial"}], stop_reason)])
    with app as client:
        run, _ = finish(client, create_task(client, _source(tmp_path)))
    assert run["status"] == "failed" and run["error"]["code"] == code


def test_claude_requires_its_api_key_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    app, _ = _claude_app(tmp_path, [])
    with pytest.raises(Exception, match="TEST_ANTHROPIC_KEY"):
        with app:
            pass
