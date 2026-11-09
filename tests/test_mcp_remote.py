"""Remote MCP servers over Streamable HTTP, with credentials read from environment variables.

A remote server is configured with a `url` and, for credentials, the *name* of an environment
variable (`bearerTokenEnv`, or `headerEnv` for other headers); the secret itself never appears
in the config, the API, events or the model's context. Only HTTPS is accepted, except plain
HTTP to a loopback address. The same allow/ask/deny rules as for local servers apply.
"""
from collections.abc import Iterator
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.mcp_hub import load_config
from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_mcp_tools import SERVER, _start
from test_workbench import finish, tool_reply

TOKEN = "remote-notes-token-0123456789"
SEARCH = "mcp__remote__search_notes"


@pytest.fixture(scope="module")
def remote_url() -> Iterator[str]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(SERVER), "--http", str(port)],
                               env={**os.environ, "NOTES_HTTP_TOKEN": TOKEN},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, "The HTTP MCP test server exited."
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


def _config(tmp_path: Path, entry: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remote": {"allowedTools": ["search_notes"], **entry}}}), encoding="utf-8")
    return path


def _app(tmp_path: Path, entry: dict, replies: list[httpx.Response] | None = None) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer(replies or [])),
                                 mcp_config=_config(tmp_path, entry)))


def test_a_remote_server_connects_with_a_token_from_the_environment(tmp_path: Path, remote_url: str,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_NOTES_TOKEN", TOKEN)
    replies = [
        tool_reply("plan_update", {"steps": ["Search the notes", "Publish the owner"]}, "plan"),
        tool_reply(SEARCH, {"query": "handoff"}, "search"),
        tool_reply("artifact_write", {"name": "owner.md", "content": "Owner: Mei [mcp/remote/search_notes E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved owner.md: Mei owns the support handoff."}, "finish"),
    ]
    with _app(tmp_path, {"url": remote_url, "bearerTokenEnv": "REMOTE_NOTES_TOKEN"}, replies) as client:
        config = client.get("/api/config").json()
        remote = config["mcp"]["servers"]["remote"]
        final, events = finish(client, _start(client, tmp_path, mcp_tools=[SEARCH]))

    assert remote["status"] == "connected", remote
    assert [tool["name"] for tool in remote["tools"]] == [SEARCH]
    assert remote["transport"] == "http" and remote["url"] == remote_url
    assert final["status"] == "completed", final["error"]
    searched = next(event["data"]["result"] for event in events
                    if event["kind"] == "tool.completed" and event["data"]["tool"] == SEARCH)
    assert "Owner: Mei" in searched["text"] and searched["path"] == "mcp/remote/search_notes"
    assert TOKEN not in json.dumps(config) and TOKEN not in json.dumps(events)


def test_a_missing_or_wrong_token_leaves_the_server_unavailable(tmp_path: Path, remote_url: str,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMOTE_NOTES_TOKEN", raising=False)
    with _app(tmp_path / "missing", {"url": remote_url, "bearerTokenEnv": "REMOTE_NOTES_TOKEN"}) as client:
        missing = client.get("/api/config").json()["mcp"]["servers"]["remote"]
    monkeypatch.setenv("REMOTE_NOTES_TOKEN", "a-wrong-token-9876543210")
    with _app(tmp_path / "wrong", {"url": remote_url, "bearerTokenEnv": "REMOTE_NOTES_TOKEN"}) as client:
        wrong = client.get("/api/config").json()["mcp"]["servers"]["remote"]

    assert missing["status"] == "unavailable" and "REMOTE_NOTES_TOKEN" in missing["error"] and not missing["tools"]
    assert wrong["status"] == "unavailable" and not wrong["tools"]
    assert "a-wrong-token" not in json.dumps(wrong)


def test_custom_headers_also_come_from_the_environment(tmp_path: Path, remote_url: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_NOTES_AUTH", f"Bearer {TOKEN}")
    with _app(tmp_path, {"url": remote_url, "headerEnv": {"Authorization": "REMOTE_NOTES_AUTH"}}) as client:
        remote = client.get("/api/config").json()["mcp"]["servers"]["remote"]
    assert remote["status"] == "connected", remote


@pytest.mark.parametrize(("entry", "message"), [
    ({"url": "http://mcp.example.com/mcp"}, "https"),
    ({"url": "ftp://127.0.0.1/mcp"}, "https"),
    ({"url": "https://mcp.example.com/mcp", "command": "notes"}, "either"),
    ({"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer literal"}}, "unknown"),
    ({"url": "https://mcp.example.com/mcp", "headerEnv": {"Host": "SOME_ENV"}}, "header"),
    ({"url": "https://mcp.example.com/mcp", "headerEnv": {"X-Key": "not an env name"}}, "environment variable"),
    ({"url": "https://mcp.example.com/mcp", "bearerTokenEnv": ""}, "environment variable"),
])
def test_remote_configs_are_validated(tmp_path: Path, entry: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(_config(tmp_path, entry))


def test_https_and_loopback_http_urls_are_accepted(tmp_path: Path) -> None:
    for url in ("https://mcp.example.com/mcp", "http://127.0.0.1:9000/mcp", "http://localhost:9000/mcp", "http://[::1]:9000/mcp"):
        assert load_config(_config(tmp_path, {"url": url}))[0].url == url
