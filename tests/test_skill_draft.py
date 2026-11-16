"""A skill drafted from a completed Run: the method that worked, written down for review.

`GET /api/runs/{id}/skill-draft?name=...` returns a SKILL.md built from the Run's own events: the kinds
of sources and options it used, the order of tool calls, what was sent back and corrected, what needed
approval and what it returned. It is deterministic (no model call), contains no source or answer text,
and is a draft: one Run shows what was done, not the rules behind it. The workbench does not write it
anywhere; saving it into the skills folder is the user's step.
"""
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.skills import SkillLibrary
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply


def _run(tmp_path: Path, replies: list[httpx.Response], **body: object) -> tuple[TestClient, dict]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "orders.csv").write_bytes(b"region,amount\nnorth,40\nsouth,2\n")
    (source / "notes.md").write_bytes(b"Owner: Mei\n")
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    client = TestClient(create_app(tmp_path / "store", model_settings=settings,
                                   model_transport=httpx.MockTransport(CompletionPeer(replies))))
    client.__enter__()
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    run = client.post(f"/api/sessions/{session['id']}/runs",
                      json={"task": "What is the total amount, and who owns it?", "sources": ["orders.csv", "notes.md"], **body}).json()
    final, _ = finish(client, run)
    return client, final


def test_a_completed_run_becomes_a_loadable_skill_draft(tmp_path: Path) -> None:
    client, final = _run(tmp_path, [
        tool_reply("plan_update", {"steps": ["Inspect the table", "Sum it", "Read the notes", "Answer"]}, "plan"),
        tool_reply("table_inspect", {"path": "orders.csv"}, "inspect"),
        tool_reply("table_aggregate", {"path": "orders.csv", "column": "amount", "operation": "sum"}, "sum"),
        tool_reply("source_read", {"path": "notes.md", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "The total is 42 and Mei owns it."}, "uncited"),
        tool_reply("task_finish", {"summary": "The total is 42 [orders.csv E2] and Mei owns it [notes.md E3]."}, "finish"),
    ], completion_workflow="answer")
    assert final["status"] == "completed", final["error"]
    response = client.get(f"/api/runs/{final['id']}/skill-draft", params={"name": "total-and-owner"})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["name"] == "total-and-owner" and draft["file"] == "total-and-owner/SKILL.md"
    content = draft["content"]

    folder = tmp_path / "skills" / "total-and-owner"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(content.encode("utf-8"))
    library = SkillLibrary.from_directory(tmp_path / "skills")
    assert library.excluded == [] and [skill["name"] for skill in library.status()["available"]] == ["total-and-owner"]

    assert "What is the total amount, and who owns it?" in content
    assert f"Run {final['id']}" in content and "draft" in content.lower()
    steps = content.split("## Steps that worked")[1].split("##")[0]
    assert [line.split(". ", 1)[1] for line in steps.strip().splitlines()] == [
        "plan_update: Inspect the table → Sum it → Read the notes → Answer",
        "table_inspect on a .csv source",
        "table_aggregate on a .csv source (column, operation)",
        "source_read on a .md source",
        "task_finish with a cited answer",
    ]
    assert "task_finish was sent back once: missing_answer_citation" in content
    assert "No approval was needed" in content and "A direct answer" in content
    # Nothing from the sources or the answer is copied into the skill.
    assert "Mei" not in content and "The total is" not in content and "north" not in content
    assert client.get(f"/api/runs/{final['id']}/skill-draft", params={"name": "total-and-owner"}).json() == draft  # deterministic
    client.__exit__(None, None, None)


def test_a_draft_needs_a_valid_name_and_a_completed_run(tmp_path: Path) -> None:
    client, final = _run(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read"]}, "plan"),
        tool_reply("source_read", {"path": "missing.md", "labels": []}, "read"),
    ])
    assert final["status"] == "failed"
    refused = client.get(f"/api/runs/{final['id']}/skill-draft", params={"name": "anything"})
    assert refused.status_code == 409 and "completed" in refused.text
    assert client.get(f"/api/runs/{final['id']}/skill-draft", params={"name": "Not Valid"}).status_code == 422
    assert client.get("/api/runs/missing/skill-draft", params={"name": "ok"}).status_code == 404
    client.__exit__(None, None, None)
