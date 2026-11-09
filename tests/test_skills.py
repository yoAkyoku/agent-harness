"""User-installed skills: SKILL.md folders whose descriptions sit in context and whose bodies load on demand."""
import json
from pathlib import Path
import subprocess
import sys

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_mcp_tools import _start
from test_workbench import finish, tool_reply

BODY_MARKER = "List each decision on its own line"


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "meeting-notes").mkdir(parents=True)
    (root / "meeting-notes" / "SKILL.md").write_text(
        "---\nname: meeting-notes\ndescription: >-\n  Summarise meeting notes into decisions and owners.\n"
        "  Use when a task asks for meeting minutes.\nlicense: MIT\n---\n\n# Meeting notes\n\n"
        f"{BODY_MARKER}, followed by its owner.\n", encoding="utf-8")
    (root / "Bad_Name").mkdir()
    (root / "Bad_Name" / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: Bad.\n---\nBody\n", encoding="utf-8")
    (root / "mismatch").mkdir()
    (root / "mismatch" / "SKILL.md").write_text("---\nname: other-name\ndescription: Wrong folder.\n---\nBody\n", encoding="utf-8")
    (root / "empty").mkdir()
    (root / "huge").mkdir()
    (root / "huge" / "SKILL.md").write_text("---\nname: huge\ndescription: Too big.\n---\n" + "x" * 40_000, encoding="utf-8")
    return root


def _app(tmp_path: Path, replies: list[httpx.Response]) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer),
                                 skills_dir=_skills(tmp_path))), peer


def test_config_lists_valid_skills_and_explains_exclusions(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, [])
    with app as client:
        skills = client.get("/api/config").json()["skills"]
    assert skills["available"] == [{"name": "meeting-notes", "description":
                                    "Summarise meeting notes into decisions and owners. Use when a task asks for meeting minutes."}]
    assert {item["folder"]: item["reason"] for item in skills["excluded"]} == {
        "Bad_Name": "invalid_name", "empty": "missing_skill_md", "huge": "body_too_large", "mismatch": "name_mismatch"}


def test_selected_skill_is_described_up_front_and_loaded_only_on_request(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": ["Load the skill", "Read", "Publish"]}, "plan"),
        tool_reply("skill_load", {"name": "meeting-notes"}, "skill"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "minutes.md", "content": "Revenue was 42. [facts.md E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved minutes.md with revenue 42."}, "finish"),
    ])
    with app as client:
        run = _start(client, tmp_path, skills=["meeting-notes"])
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    first = peer.requests[0]
    assert "meeting-notes" in first["messages"][0]["content"]
    assert "Summarise meeting notes" in first["messages"][0]["content"]
    assert BODY_MARKER not in json.dumps(first)  # the body is not in context until loaded
    assert "skill_load" in {tool["function"]["name"] for tool in peer.requests[1]["tools"]}
    loaded = next(event["data"]["result"] for event in events
                  if event["kind"] == "tool.completed" and event["data"]["tool"] == "skill_load")
    assert BODY_MARKER in loaded["instructions"] and len(loaded["sha256"]) == 64
    assert BODY_MARKER in json.dumps(peer.requests[2]["messages"])


def test_runs_without_skills_get_no_catalog_or_loader(tmp_path: Path) -> None:
    app, peer = _app(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "r.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved r.md with revenue 42."}, "finish"),
    ])
    with app as client:
        final, _ = finish(client, _start(client, tmp_path))
    assert final["status"] == "completed", final["error"]
    assert "meeting-notes" not in json.dumps(peer.requests)
    assert not any(tool["function"]["name"] == "skill_load" for request in peer.requests for tool in request["tools"])


def test_unknown_or_excluded_skills_are_refused_at_admission(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, [])
    with app as client:
        source = tmp_path / "source"
        source.mkdir()
        (source / "facts.md").write_bytes(b"Revenue was 42.\n")
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        for name in ("huge", "not-installed"):
            response = client.post(f"/api/sessions/{session['id']}/runs",
                                   json={"task": "Summarise.", "sources": ["facts.md"], "skills": [name]})
            assert response.status_code == 422, (name, response.text)
            assert response.json()["detail"]["code"] == "skill_unavailable"


def test_cli_rejects_a_missing_skills_directory(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-m", "agent_harness", "--store", str(tmp_path / "store"),
                             "--skills-dir", str(tmp_path / "nowhere")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "skills directory" in result.stderr and "does not exist" in result.stderr
