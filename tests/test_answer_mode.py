"""Answer mode: a Run that answers directly, without publishing a file, and still proves its facts.

With `completion_workflow: "answer"` the model is not offered `artifact_write`; it ends with the
answer itself (through `task_finish` or as plain text). The answer must cite the evidence it uses
(`[source EID]`), every cited ID must exist, and the same verifiers as for artifacts run on it.
The verified answer is the Run's result, recorded with the evidence it cites.
"""
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, text_reply, tool_reply


def _client(tmp_path: Path, replies: list[httpx.Response], *, thinking: bool = True) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=thinking)
    return TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))), peer


def _start(client: TestClient, tmp_path: Path, **body: object) -> httpx.Response:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\nOwner: Mei\n")
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    return client.post(f"/api/sessions/{session['id']}/runs",
                       json={"task": "Who owns it and what was the revenue?", "sources": ["facts.md"], **body})


def _offered(request: dict) -> list[str]:
    return [tool["function"]["name"] for tool in request.get("tools") or []]


def test_a_cited_answer_completes_the_run_without_an_artifact(tmp_path: Path) -> None:
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read facts.md", "Answer with citations"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "Mei owns it [facts.md E1], and revenue was 42 [facts.md E1]."}, "finish"),
    ])
    with app as client:
        response = _start(client, tmp_path, completion_workflow="answer")
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())

    assert final["status"] == "completed", final["error"]
    assert final["result"] == "Mei owns it [facts.md E1], and revenue was 42 [facts.md E1]."
    assert final["artifacts"] == [] and final["policy"]["completion_workflow"] == "answer"
    verified = next(event["data"] for event in events if event["kind"] == "answer.verified")
    assert [(item["evidence_id"], item["path"]) for item in verified["cited"]] == [("E1", "facts.md")]
    assert len(verified["cited"][0]["sha256"]) == 64
    assert all("artifact_write" not in _offered(request) for request in peer.requests)
    assert "task_finish" in _offered(peer.requests[-1])


def test_an_answer_without_citations_is_sent_back(tmp_path: Path) -> None:
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "Mei owns it and revenue was 42."}, "uncited"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E9]."}, "unknown-id"),
        tool_reply("task_finish", {"summary": "Mei owns it; revenue was 42 [facts.md E1]."}, "cited"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path, completion_workflow="answer", limits={"max_corrections": 3}).json())

    assert final["status"] == "completed", final["error"]
    codes = [event["data"]["error"]["code"] for event in events if event["kind"] == "correction.available"]
    assert codes == ["missing_answer_citation", "invalid_evidence"]


def test_a_plain_text_ending_is_checked_as_the_answer(tmp_path: Path) -> None:
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        text_reply("Revenue was 42 [facts.md E1]."),
    ], thinking=False)
    with app as client:
        final, events = finish(client, _start(client, tmp_path, completion_workflow="answer").json())

    assert final["status"] == "completed", final["error"]
    assert final["result"] == "Revenue was 42 [facts.md E1]." and final["artifacts"] == []
    assert any(event["kind"] == "answer.verified" for event in events)
    assert not any(event["kind"] == "publication.recovery_requested" for event in events)


def test_answer_mode_is_refused_where_it_cannot_work(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "fixture-store")) as client:  # the scripted fixture cannot write cited answers
        fixture = _start(client, tmp_path, completion_workflow="answer")
    app, _ = _client(tmp_path / "model", [])
    with app as client:
        drafted = _start(client, tmp_path / "model", completion_workflow="answer", publication_workflow="draft_review")

    assert fixture.status_code == 422 and fixture.json()["detail"]["code"] == "unsupported_workflow"
    assert drafted.status_code == 422 and drafted.json()["detail"]["code"] == "unsupported_workflow"
