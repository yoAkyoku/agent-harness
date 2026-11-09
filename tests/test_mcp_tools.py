"""Read-only tools from user-configured MCP servers, over a real stdio server process.

Only servers in the MCP config are started and only tools listed in `allowedTools` are
offered; a tool whose annotations declare side effects is refused even when listed.
A Run opts into specific MCP tools, and their results become citable evidence.
"""
import json
import os
from pathlib import Path
import sys

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

SERVER = Path(__file__).with_name("mcp_notes_server.py")
SEARCH = "mcp__notes__search_notes"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"notes": {
        "command": sys.executable, "args": [str(SERVER)],
        "allowedTools": ["search_notes", "delete_note", "lookup_note", "env_has"],
    }}}), encoding="utf-8")
    return path


def _app(tmp_path: Path, replies: list[httpx.Response]) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer),
                     mcp_config=_config(tmp_path))
    return TestClient(app), peer


def _start(client: TestClient, tmp_path: Path, **run: object) -> dict:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    response = client.post(f"/api/sessions/{session['id']}/runs",
                           json={"task": "Who owns the support handoff?", "sources": ["facts.md"], **run})
    assert response.status_code == 202, response.text
    return response.json()


def test_only_allowed_read_only_tools_are_offered(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, [])
    with app as client:
        notes = client.get("/api/config").json()["mcp"]["servers"]["notes"]

    assert notes["status"] == "connected"
    assert [tool["name"] for tool in notes["tools"]] == ["mcp__notes__env_has", "mcp__notes__lookup_note", SEARCH]
    excluded = {item["tool"]: item["reason"] for item in notes["excluded"]}
    assert excluded == {"create_note": "not_allowed", "delete_note": "declares_side_effects", "word_count": "not_allowed"}


def test_selected_mcp_tool_result_is_citable_evidence(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": ["Search the notes", "Publish"]}, "plan"),
        tool_reply(SEARCH, {"query": "handoff"}, "search"),
        tool_reply("artifact_write", {"name": "owner.md", "content": "Owner: Mei [mcp/notes/search_notes E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved owner.md: Mei owns the handoff."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, mcp_tools=[SEARCH])
        final, events = finish(client, run)
        assert final["status"] == "completed", final["error"]
        artifact = client.get(final["artifacts"][0]["download_url"]).text

    offered = [tool["function"]["name"] for tool in peer.requests[1]["tools"]]
    assert SEARCH in offered and "mcp__notes__lookup_note" not in offered  # only the Run's selection
    description = next(tool["function"]["description"] for tool in peer.requests[1]["tools"]
                       if tool["function"]["name"] == SEARCH)
    assert "untrusted" in description.lower()
    completed = next(event["data"] for event in events
                     if event["kind"] == "tool.completed" and event["data"]["tool"] == SEARCH)
    assert completed["result"]["evidence_id"] == "E1"
    assert "Owner: Mei" in completed["result"]["text"]
    dispatched = next(event["data"] for event in events
                      if event["kind"] == "tool.dispatched" and event["data"]["tool"] == SEARCH)
    assert dispatched["effect"] == "read_only"
    assert "[E1] mcp/notes/search_notes:L1-L" in artifact


def test_mcp_tool_errors_return_to_the_model_as_correctable(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": ["Look up the note", "Publish"]}, "plan"),
        tool_reply("mcp__notes__lookup_note", {"name": "missing"}, "lookup"),
        tool_reply("mcp__notes__lookup_note", {"name": "handoff"}, "lookup-again"),
        tool_reply("artifact_write", {"name": "owner.md", "content": "Owner: Mei [mcp/notes/lookup_note E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved owner.md."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, mcp_tools=["mcp__notes__lookup_note"])
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    feedback = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert feedback["tool"] == "mcp__notes__lookup_note" and feedback["error"]["code"] == "mcp_tool_error"
    assert "No note named missing" in feedback["error"]["message"]
    assert any(event["kind"] == "correction.observed" for event in events)


def test_runs_cannot_select_unavailable_or_excluded_mcp_tools(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, [])
    with app as client:
        source = tmp_path / "source"
        source.mkdir()
        (source / "facts.md").write_bytes(b"Revenue was 42.\n")
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        for name in ("mcp__notes__delete_note", "mcp__notes__word_count", "mcp__other__search"):
            response = client.post(f"/api/sessions/{session['id']}/runs",
                                   json={"task": "Delete notes.", "sources": ["facts.md"], "mcp_tools": [name]})
            assert response.status_code == 422, (name, response.text)
            assert response.json()["detail"]["code"] == "mcp_tool_unavailable"


def test_a_run_that_selects_no_mcp_tools_is_offered_none(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "r.md", "content": "Revenue was 42. [facts.md E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved r.md with revenue 42."}, "finish"),
    ])
    with app as client:
        final, _ = finish(client, _start(client, tmp_path))
    assert final["status"] == "completed", final["error"]
    assert not any(tool["function"]["name"].startswith("mcp__") for request in peer.requests for tool in request["tools"])


def test_resume_restores_confirmed_mcp_evidence(tmp_path: Path) -> None:
    import asyncio

    count = 0

    async def interrupted(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.url.path != "/v1/chat/completions":
            return await CompletionPeer([])(request)
        count += 1
        if count == 1:
            return tool_reply("plan_update", {"steps": ["Search", "Publish"]}, "plan")
        if count == 2:
            return tool_reply(SEARCH, {"query": "handoff"}, "search")
        await asyncio.Event().wait()  # the service stops before the next decision
        raise AssertionError("unreachable")

    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    store = tmp_path / "store"
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(interrupted),
                               mcp_config=_config(tmp_path))) as first:
        run = _start(first, tmp_path, mcp_tools=[SEARCH])
        import time
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # the search is confirmed and the next decision has started
            events = first.get(f"/api/runs/{run['id']}/events").json()["events"]
            searched = any(e["kind"] == "tool.completed" and e["data"]["tool"] == SEARCH for e in events)
            if searched and any(e["kind"] == "model.requested" and e["data"]["step"] == 3 for e in events):
                break
            time.sleep(.02)
        else:
            raise AssertionError("The MCP search was not confirmed before the service stopped.")
    app, peer = _app(tmp_path, [
        tool_reply("artifact_write", {"name": "owner.md", "content": "Owner: Mei [mcp/notes/search_notes E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved owner.md: Mei owns the handoff."}, "finish"),
    ])
    with app as second:
        checkpoint = second.get(f"/api/runs/{run['id']}/checkpoint").json()
        assert [call["tool"] for call in checkpoint["confirmed"]][-1] == SEARCH
        response = second.post(f"/api/runs/{run['id']}/resume",
                               json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-mcp-01"})
        assert response.status_code == 202, response.text
        final, events = finish(second, response.json())

    assert final["status"] == "completed", final["error"]
    loaded = next(event["data"] for event in events if event["kind"] == "checkpoint.loaded")
    assert loaded["restored_evidence_ids"] == ["E1"]


def test_cli_rejects_an_mcp_config_without_an_allowlist(tmp_path: Path) -> None:
    import subprocess

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mcpServers": {"notes": {"command": sys.executable, "args": [str(SERVER)]}}}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "agent_harness", "--store", str(tmp_path / "store"),
                             "--mcp-config", str(bad)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "allowedTools" in result.stderr


def test_headless_run_on_the_cli_workbench_can_select_an_mcp_tool(tmp_path: Path) -> None:
    import subprocess
    from test_headless import _headless, _source
    from test_recovery import running_service

    with running_service(tmp_path / "store", "--mcp-config", str(_config(tmp_path))) as service:
        assert service.client.get("/api/config").json()["mcp"]["servers"]["notes"]["status"] == "connected"
        result = _headless(str(service.client.base_url), "--workspace", str(_source(tmp_path)),
                           "--source", "facts.md", "--task", "Save a source snapshot.", "--mcp-tool", SEARCH)

    assert result.returncode == 0, result.stderr
    accepted = next(json.loads(line) for line in result.stdout.splitlines() if '"run.accepted"' in line)
    assert accepted["data"]["policy"]["mcp_tools"] == [SEARCH]


def test_workbench_secrets_do_not_reach_mcp_servers(tmp_path: Path) -> None:
    assert os.environ.get("AGENT_HARNESS_TOKEN")  # set for every test by conftest
    app, _ = _app(tmp_path, [
        tool_reply("plan_update", {"steps": ["Check", "Publish"]}, "plan"),
        tool_reply("mcp__notes__env_has", {"name": "AGENT_HARNESS_TOKEN"}, "check"),
    ])
    with app as client:
        run = _start(client, tmp_path, mcp_tools=["mcp__notes__env_has"], limits={"max_steps": 2})
        _, events = finish(client, run)

    result = next(event["data"]["result"] for event in events
                  if event["kind"] == "tool.completed" and event["data"]["tool"] == "mcp__notes__env_has")
    assert result["text"] == "absent"
