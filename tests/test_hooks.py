"""Hooks: user-configured HTTP webhooks before and after tool calls (no shell commands).

A `before_tool` hook answers allow or deny for each matching call; a deny (from any hook) refuses
the call and returns the reason to the model, and a hook that times out, fails or answers
anything else counts as a deny (fail closed). An `after_tool` hook is notified of completed
calls and cannot change the Run; its failures are recorded only. Hooks never change arguments,
and the verifiers do not consult them.
"""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.hooks import load_hooks
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

WRITE = {"name": "revenue.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}


def _config(tmp_path: Path, hooks: list[dict]) -> Path:
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return path


class HookPeer:
    """A webhook endpoint: records each request and answers with `decide(payload)`."""

    def __init__(self, decide=lambda payload: {"decision": "allow"}) -> None:  # type: ignore[no-untyped-def]
        self.requests: list[tuple[str, dict, dict]] = []
        self.decide = decide

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append((str(request.url), dict(request.headers), payload))
        answer = self.decide(payload)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)


def _run(tmp_path: Path, hooks: list[dict], replies: list[httpx.Response], peer: HookPeer) -> tuple[dict, list[dict], dict]:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    (source / "secret.md").write_bytes(b"Do not read.\n")
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(CompletionPeer(replies)),
                     hooks_config=_config(tmp_path, hooks), hooks_transport=httpx.MockTransport(peer))
    with TestClient(app) as client:
        config = client.get("/api/config").json()
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs",
                          json={"task": "Report the revenue.", "sources": ["facts.md", "secret.md"]}).json()
        final, events = finish(client, run)
    return final, events, config


def _replies(*reads: str) -> list[httpx.Response]:
    return [tool_reply("plan_update", {"steps": ["Read the facts", "Publish the revenue"]}, "plan"),
            *(tool_reply("source_read", {"path": path, "labels": []}, f"read-{index}") for index, path in enumerate(reads)),
            tool_reply("artifact_write", WRITE, "write"),
            tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish")]


def test_allowed_calls_run_and_each_decision_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOK_TOKEN", "hook-secret-0123456789")
    peer = HookPeer()
    final, events, config = _run(tmp_path, [{"event": "before_tool", "url": "https://hooks.example.com/check",
                                             "bearerTokenEnv": "HOOK_TOKEN"}], _replies("facts.md"), peer)

    assert final["status"] == "completed", final["error"]
    assert [payload["tool"] for _, _, payload in peer.requests] == ["plan_update", "source_read", "artifact_write", "task_finish"]
    url, headers, payload = peer.requests[1]
    assert url == "https://hooks.example.com/check" and headers["authorization"] == "Bearer hook-secret-0123456789"
    assert payload["event"] == "before_tool" and payload["run_id"] == final["id"]
    # The canonical arguments that will be dispatched (empty labels are dropped).
    assert payload["arguments"] == {"path": "facts.md"} and payload["effect"] == "read_only"
    decided = [event["data"] for event in events if event["kind"] == "hook.decided"]
    assert [item["decision"] for item in decided] == ["allow"] * 4
    assert config["hooks"] == [{"event": "before_tool", "url": "https://hooks.example.com/check", "tools": ["*"]}]
    assert "hook-secret" not in json.dumps(config) and "hook-secret" not in json.dumps(events)


def test_a_denied_call_never_runs_and_the_model_gets_the_reason(tmp_path: Path) -> None:
    def decide(payload: dict) -> dict:
        if payload["arguments"].get("path") == "secret.md":
            return {"decision": "deny", "reason": "secret.md is off limits for reports."}
        return {"decision": "allow"}

    replies = _replies("secret.md", "facts.md")
    final, events, _ = _run(tmp_path, [{"event": "before_tool", "url": "http://127.0.0.1:9/check", "tools": ["source_read"]}],
                            replies, HookPeer(decide))

    assert final["status"] == "completed", final["error"]
    assert not any(event["kind"] == "tool.dispatched" and event["data"]["arguments"].get("path") == "secret.md" for event in events)
    correction = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert correction["error"]["code"] == "hook_denied"
    assert "secret.md is off limits for reports." in correction["error"]["message"]
    decided = [(event["data"]["tool"], event["data"]["decision"]) for event in events if event["kind"] == "hook.decided"]
    assert decided == [("source_read", "deny"), ("source_read", "allow")]  # only matching tools are sent


@pytest.mark.parametrize("failure", ["error_status", "not_json", "bad_decision", "unreachable"])
def test_a_failing_hook_denies(tmp_path: Path, failure: str) -> None:
    def decide(payload: dict):  # type: ignore[no-untyped-def]
        if failure == "error_status":
            return httpx.Response(500, text="boom")
        if failure == "not_json":
            return httpx.Response(200, text="allow")
        if failure == "bad_decision":
            return {"decision": "maybe"}
        raise httpx.ConnectError("refused")

    final, events, _ = _run(tmp_path, [{"event": "before_tool", "url": "https://hooks.example.com/check", "tools": ["source_read"]}],
                            _replies("facts.md", "facts.md", "facts.md")[:4], HookPeer(decide))

    assert final["status"] == "failed" and final["error"]["code"] == "correction_limit_exceeded"
    assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "source_read" for event in events)
    assert all(event["data"]["decision"] == "deny" for event in events if event["kind"] == "hook.decided")


def test_a_missing_credential_denies_without_calling_the_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOK_TOKEN", raising=False)
    peer = HookPeer()
    final, events, _ = _run(tmp_path, [{"event": "before_tool", "url": "https://hooks.example.com/check", "tools": ["source_read"],
                                        "bearerTokenEnv": "HOOK_TOKEN"}], _replies("facts.md", "facts.md", "facts.md")[:4], peer)

    assert final["status"] == "failed" and not peer.requests
    reason = next(event["data"]["reason"] for event in events if event["kind"] == "hook.decided")
    assert "HOOK_TOKEN" in reason


def test_after_tool_hooks_are_notified_and_their_failures_do_not_stop_the_run(tmp_path: Path) -> None:
    calls: list[str] = []

    def decide(payload: dict):  # type: ignore[no-untyped-def]
        calls.append(payload["tool"])
        if payload["tool"] == "artifact_write":
            raise httpx.ConnectError("down")
        return httpx.Response(204)

    final, events, _ = _run(tmp_path, [{"event": "after_tool", "url": "https://audit.example.com/log",
                                        "tools": ["source_read", "artifact_write"]}], _replies("facts.md"), HookPeer(decide))

    assert final["status"] == "completed", final["error"]
    assert calls == ["source_read", "artifact_write"]
    notified = [(event["data"]["tool"], event["data"]["status"]) for event in events if event["kind"] == "hook.notified"]
    assert notified == [("source_read", "delivered"), ("artifact_write", "failed")]


def test_after_tool_payload_carries_a_result_summary_not_the_text(tmp_path: Path) -> None:
    peer = HookPeer(lambda payload: httpx.Response(204))
    _run(tmp_path, [{"event": "after_tool", "url": "https://audit.example.com/log", "tools": ["source_read"]}],
         _replies("facts.md"), peer)
    payload = peer.requests[0][2]
    assert payload["event"] == "after_tool" and payload["tool"] == "source_read"
    assert payload["result"]["path"] == "facts.md" and payload["result"]["evidence_id"] == "E1"
    assert "text" not in payload["result"] and len(payload["result"]["sha256"]) == 64


@pytest.mark.parametrize(("hook", "message"), [
    ({"event": "before_tool", "command": "python check.py"}, "shell"),
    ({"event": "before_tool"}, "url"),
    ({"event": "on_start", "url": "https://hooks.example.com"}, "event"),
    ({"event": "before_tool", "url": "http://hooks.example.com"}, "https"),
    ({"event": "before_tool", "url": "https://hooks.example.com", "tools": "source_read"}, "tools"),
    ({"event": "before_tool", "url": "https://hooks.example.com", "timeoutSeconds": 120}, "timeoutSeconds"),
    ({"event": "before_tool", "url": "https://hooks.example.com", "headers": {"X-Key": "literal"}}, "unknown"),
])
def test_hook_configs_are_validated(tmp_path: Path, hook: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_hooks(_config(tmp_path, [hook]))


def test_cli_refuses_a_shell_command_hook(tmp_path: Path) -> None:
    import subprocess
    import sys

    config = _config(tmp_path, [{"event": "before_tool", "command": "python check.py"}])
    result = subprocess.run([sys.executable, "-m", "agent_harness", "--store", str(tmp_path / "store"),
                             "--hooks-config", str(config)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "shell commands are not supported" in result.stderr
