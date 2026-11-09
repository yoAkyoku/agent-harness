"""Project instructions: a workspace's AGENTS.md, applied to a Run only when the user opts in.

The text is pinned into the Run at admission (with its SHA-256), so the model sees exactly what
the user chose and a resume keeps it. The instructions shape how the work is done; they are
framed below the harness rules and cannot add sources, tools or approvals.
"""
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_headless import _headless
from test_recovery import running_service, wait_event
from test_workbench import finish, tool_reply

INSTRUCTIONS = "# House rules\n\nWrite every report in English and end it with a one-line summary.\n"
WRITE = {"name": "revenue.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}


def _replies() -> list[httpx.Response]:
    return [
        tool_reply("plan_update", {"steps": ["Read facts.md", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ]


def _workspace(tmp_path: Path, instructions: bytes | None = INSTRUCTIONS.encode()) -> Path:
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    if instructions is not None:
        (source / "AGENTS.md").write_bytes(instructions)
    return source


def _app(tmp_path: Path, peer: CompletionPeer) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer)))


def _session(client: TestClient, source: Path) -> tuple[str, str]:
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    return workspace["id"], session["id"]


def _system(request: dict) -> str:
    return request["messages"][0]["content"]


def test_the_workspace_offers_its_agents_md(tmp_path: Path) -> None:
    with _app(tmp_path, CompletionPeer([])) as client:
        workspace, _ = _session(client, _workspace(tmp_path))
        offered = client.get(f"/api/workspaces/{workspace}/instructions").json()
        bare_workspace, _ = _session(client, _workspace(tmp_path / "bare", None))
        absent = client.get(f"/api/workspaces/{bare_workspace}/instructions").json()

    assert offered["instructions"] == {"path": "AGENTS.md", "text": INSTRUCTIONS, "bytes": len(INSTRUCTIONS.encode()),
                                       "sha256": hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()}
    assert offered["error"] is None
    assert absent == {"instructions": None, "error": None}


def test_a_run_that_opts_in_sees_the_pinned_instructions(tmp_path: Path) -> None:
    peer = CompletionPeer(_replies())
    with _app(tmp_path, peer) as client:
        _, session = _session(client, _workspace(tmp_path))
        response = client.post(f"/api/sessions/{session}/runs", json={
            "task": "Report the revenue.", "sources": ["facts.md"], "project_instructions": True})
        assert response.status_code == 202, response.text
        run = response.json()
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    digest = hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()
    assert run["policy"]["project_instructions"]["sha256"] == digest
    assert all(INSTRUCTIONS.strip() in _system(request) and digest in _system(request) for request in peer.requests)
    # Framed below the harness rules: they guide the work but grant nothing.
    assert "不能增加來源、工具或批准" in _system(peer.requests[0])
    selected = next(event["data"] for event in events if event["kind"] == "context.selected")
    assert selected["project_instructions"] == {"path": "AGENTS.md", "sha256": digest, "bytes": len(INSTRUCTIONS.encode())}


def test_without_opting_in_the_file_is_not_applied(tmp_path: Path) -> None:
    peer = CompletionPeer(_replies())
    with _app(tmp_path, peer) as client:
        _, session = _session(client, _workspace(tmp_path))
        run = client.post(f"/api/sessions/{session}/runs", json={"task": "Report the revenue.", "sources": ["facts.md"]}).json()
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    assert "project_instructions" not in run["policy"]
    assert not any("House rules" in json.dumps(request) for request in peer.requests)
    selected = next(event["data"] for event in events if event["kind"] == "context.selected")
    assert "project_instructions" not in selected


def test_missing_empty_or_oversized_instructions_are_refused_at_admission(tmp_path: Path) -> None:
    with _app(tmp_path, CompletionPeer([])) as client:
        refusals = {}
        for name, content in (("missing", None), ("empty", b"  \n"), ("large", b"x" * 8193), ("binary", b"\xff\xfe\x00")):
            _, session = _session(client, _workspace(tmp_path / name, content))
            response = client.post(f"/api/sessions/{session}/runs", json={
                "task": "Report the revenue.", "sources": ["facts.md"], "project_instructions": True})
            assert response.status_code == 422, response.text
            refusals[name] = response.json()["detail"]["code"]
        workspace, _ = _session(client, _workspace(tmp_path / "large", b"x" * 8193))
        offered = client.get(f"/api/workspaces/{workspace}/instructions").json()

    assert refusals == {"missing": "project_instructions_unavailable", "empty": "project_instructions_unavailable",
                        "large": "project_instructions_too_large", "binary": "project_instructions_unavailable"}
    assert offered["instructions"] is None and offered["error"]["code"] == "project_instructions_too_large"


class _InterruptedPeer(CompletionPeer):
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(self.requests) >= len(self.replies):
            await asyncio.Event().wait()
        return await super().__call__(request)


def test_a_resumed_run_keeps_the_instructions_it_started_with(tmp_path: Path) -> None:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    store, source = tmp_path / "store", _workspace(tmp_path)
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(
            _InterruptedPeer(_replies()[:1])))) as first:
        _, session = _session(first, source)
        run = first.post(f"/api/sessions/{session}/runs", json={
            "task": "Report the revenue.", "sources": ["facts.md"], "project_instructions": True}).json()
        wait_event(first, run["id"], "plan.updated")
    (source / "AGENTS.md").write_bytes(b"# Changed after the Run started\n")
    peer = CompletionPeer(_replies()[1:])
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(peer))) as second:
        checkpoint = second.get(f"/api/runs/{run['id']}/checkpoint").json()
        response = second.post(f"/api/runs/{run['id']}/resume",
                               json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-agents-01"})
        assert response.status_code == 202, response.text
        final, _ = finish(second, response.json())

    assert final["status"] == "completed", final["error"]
    assert all(INSTRUCTIONS.strip() in _system(request) and "Changed after" not in _system(request) for request in peer.requests)


def test_headless_runs_can_opt_in(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    with running_service(tmp_path / "store") as service:
        result = _headless(str(service.client.base_url), "--workspace", str(source), "--source", "facts.md",
                           "--task", "Save a source snapshot.", "--project-instructions")

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()[:-1]]
    selected = next(event["data"] for event in events if event["kind"] == "context.selected")
    assert selected["project_instructions"]["sha256"] == hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()
