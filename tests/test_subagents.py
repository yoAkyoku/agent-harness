"""Subagents: a Run may hand a focused sub-task to a child loop with its own, fresh context.

`subagent_run {task, sources}` is opt-in twice (the workbench is started with subagents allowed, and
the Run asks for them). The child sees only the delegated task, never the parent's conversation, and
is narrower than its parent in every way: only sources the parent's Run selected and the call named,
only read-only built-in tools, no publishing, no further delegation, and its model and tool calls are
spent from the parent's budget. Its report must pass the same citation checks as a direct answer
against the child's own evidence before the parent sees it. The parent then receives the report as
one evidence record, labelled as model-written, that lists the source versions the report was
verified against; the child's own evidence IDs are rewritten so they cannot be mistaken for the
parent's. Everything the child did is recorded in the parent Run's events.
"""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import subagent
from agent_harness.contracts import RunFailure
from agent_harness.model import ModelSettings
from agent_harness.tools import Tools
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, text_reply, tool_reply

FACTS = b"Revenue was 42.\nOwner: Mei\n"
NOTES = b"Budget was 7.\n"
DELEGATE = {"task": "What was the revenue?", "sources": ["facts.md"]}


def _client(tmp_path: Path, replies: list[httpx.Response], *, allow: bool = True, **options: object) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer),
                     allow_subagents=allow, **options)  # type: ignore[arg-type]
    return TestClient(app), peer


def _start(client: TestClient, tmp_path: Path, **body: object) -> httpx.Response:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(FACTS)
    (source / "notes.md").write_bytes(NOTES)
    (source / "other.md").write_bytes(b"Not selected.\n")
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    return client.post(f"/api/sessions/{session['id']}/runs",
                       json={"task": "Write a short report on revenue and budget.", "sources": ["facts.md", "notes.md"],
                             "subagents": True, **body})


def _offered(request: dict) -> set[str]:
    return {tool["function"]["name"] for tool in request.get("tools") or []}


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [event["data"] for event in events if event["kind"] == kind]


def _report(events: list[dict]) -> dict:
    return next(data["result"] for data in _kinds(events, "tool.completed") if data["tool"] == "subagent_run")


def test_a_verified_report_becomes_one_cited_evidence_record(tmp_path: Path) -> None:
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate the revenue question", "Publish"]}, "plan"),
        tool_reply("subagent_run", DELEGATE, "delegate"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]."}, "child-finish"),
        tool_reply("artifact_write", {"name": "report.md", "content": "Revenue was 42. [subagent E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved report.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        config = client.get("/api/config").json()
        response = _start(client, tmp_path)
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())
        content = client.get(f"/api/runs/{final['id']}/artifacts/{final['artifacts'][0]['id']}").text

    assert config["subagents"] == {"enabled": True, "max_per_run": subagent.MAX_PER_RUN}
    assert final["status"] == "completed", final["error"]
    assert final["policy"]["subagents"] is True
    report = _report(events)
    facts_sha = hashlib.sha256(FACTS).hexdigest()
    assert report["evidence_type"] == "subagent_report" and report["path"] == "subagent" and report["evidence_id"] == "E1"
    assert report["text"] == "Revenue was 42 (facts.md)."  # the child's own evidence ID is not the parent's
    assert report["sha256"] == hashlib.sha256(report["text"].encode()).hexdigest()
    assert report["sources"] == [{"path": "facts.md", "sha256": facts_sha}]
    assert report["model"] == "contract-model" and report["task"] == DELEGATE["task"]
    assert f"- [E1] subagent:L1-L1 SHA-256 {report['sha256']}; model-written subagent report, not verbatim source text; " \
           f"verified against facts.md SHA-256 {facts_sha}" in content

    # The child starts from a fresh context: the delegated task, not the parent's conversation.
    child_first, child_second = peer.requests[2], peer.requests[3]
    assert [message["role"] for message in child_first["messages"]] == ["system", "user"]
    assert child_first["messages"][1]["content"] == DELEGATE["task"]
    assert "Write a short report" not in json.dumps(child_first["messages"])
    assert _offered(child_first) == {"source_read"} and _offered(child_second) == {"source_read", "task_finish"}
    assert "subagent_run" in _offered(peer.requests[1]) and "subagent_run" not in _offered(peer.requests[0])

    # Everything the child did is in the parent Run's events, and is spent from the parent's budget.
    started = _kinds(events, "subagent.started")
    assert started == [{"call_id": "delegate", "task": DELEGATE["task"],
                        "sources": [{"path": "facts.md", "sha256": facts_sha}]}]
    assert [data["tool"] for data in _kinds(events, "subagent.tool_completed")] == ["source_read"]
    assert len(_kinds(events, "subagent.model_responded")) == 2
    assert _kinds(events, "subagent.completed")[0]["call_id"] == "delegate"
    assert final["usage"] == {"model_calls": 6, "tool_calls": 6}


def test_the_child_reads_only_the_sources_it_was_given_and_cannot_delegate(tmp_path: Path) -> None:
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate", "Publish"]}, "plan"),
        tool_reply("subagent_run", DELEGATE, "delegate"),
        tool_reply("source_read", {"path": "notes.md"}, "child-forbidden"),  # selected by the Run, not given to the child
        tool_reply("subagent_run", DELEGATE, "child-nested"),
        tool_reply("artifact_write", {"name": "x.md", "content": "x", "citations": []}, "child-write"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        text_reply("Revenue was 42 [facts.md E1]."),  # a plain-text ending is the report too
        tool_reply("artifact_write", {"name": "report.md", "content": "Revenue was 42. [subagent E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved report.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path, limits={"max_corrections": 3}).json())

    assert final["status"] == "completed", final["error"]
    refused = [(data["child_call_id"], data["code"]) for data in _kinds(events, "subagent.tool_refused")]
    assert refused == [("child-forbidden", "source_forbidden"), ("child-nested", "tool_not_offered"),
                       ("child-write", "tool_not_offered")]
    assert all(_offered(request) <= {"source_read", "task_finish"} for request in peer.requests[2:7])
    assert _report(events)["text"] == "Revenue was 42 (facts.md)."
    assert final["artifacts"] and len(final["artifacts"]) == 1


def test_delegating_a_source_the_run_did_not_select_is_refused(tmp_path: Path) -> None:
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate"]}, "plan"),
        tool_reply("subagent_run", {"task": "Read it.", "sources": ["other.md"]}, "delegate"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path).json())

    assert final["status"] == "failed" and final["error"]["code"] == "source_forbidden"
    assert not _kinds(events, "subagent.started")


def test_an_uncited_report_never_reaches_the_parent(tmp_path: Path) -> None:
    uncited = {"summary": "Revenue was 42."}
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate", "Publish"]}, "plan"),
        tool_reply("subagent_run", DELEGATE, "delegate"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        tool_reply("task_finish", uncited, "child-1"),
        tool_reply("task_finish", uncited, "child-2"),
        tool_reply("task_finish", uncited, "child-3"),
        # The parent is told the sub-task failed and does the work itself.
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "report.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved report.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path).json())

    assert final["status"] == "completed", final["error"]
    failed = next(data for data in _kinds(events, "tool.failed") if data["tool"] == "subagent_run")
    assert failed["code"] == "subagent_failed"
    assert _kinds(events, "subagent.failed")[0]["reason"] == "missing_answer_citation"
    assert not any(data["tool"] == "subagent_run" for data in _kinds(events, "tool.completed"))
    correction = next(data for data in _kinds(events, "correction.available") if data["tool"] == "subagent_run")
    assert correction["error"]["retryable"] is True
    read = next(data["result"] for data in _kinds(events, "tool.completed") if data["tool"] == "source_read")
    assert read["evidence_id"] == "E1"  # the failed sub-task left no evidence behind


def test_a_direct_answer_records_what_a_cited_report_was_verified_against(tmp_path: Path) -> None:
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate", "Answer"]}, "plan"),
        tool_reply("subagent_run", DELEGATE, "delegate"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]."}, "child-finish"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [subagent E1]."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path, completion_workflow="answer").json())

    assert final["status"] == "completed", final["error"]
    cited = _kinds(events, "answer.verified")[0]["cited"][0]
    assert cited["path"] == "subagent"
    assert cited["subagent_sources"] == [{"path": "facts.md", "sha256": hashlib.sha256(FACTS).hexdigest()}]


def test_the_number_of_subagents_per_run_is_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent, "MAX_PER_RUN", 1)
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate twice", "Publish"]}, "plan"),
        tool_reply("subagent_run", DELEGATE, "first"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]."}, "child-finish"),
        tool_reply("subagent_run", {"task": "And the budget?", "sources": ["notes.md"]}, "second"),
        tool_reply("artifact_write", {"name": "report.md", "content": "Revenue was 42. [subagent E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved report.md: revenue was 42."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path).json())

    assert final["status"] == "completed", final["error"]
    rejected = next(data for data in _kinds(events, "tool.rejected") if data["call_id"] == "second")
    assert rejected["code"] == "subagent_limit_exceeded"
    assert len(_kinds(events, "subagent.started")) == 1


def test_subagents_need_the_workbench_and_the_run_to_opt_in(tmp_path: Path) -> None:
    replies = [
        tool_reply("plan_update", {"steps": ["Read", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "report.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved report.md: revenue was 42."}, "finish"),
    ]
    off, _ = _client(tmp_path / "off", [], allow=False)
    with off as client:
        refused = _start(client, tmp_path)
        assert client.get("/api/config").json()["subagents"] == {"enabled": False}
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "subagents_unavailable"

    on, peer = _client(tmp_path / "on", replies)
    with on as client:
        final, _ = finish(client, _start(client, tmp_path, subagents=False).json())
    assert final["status"] == "completed", final["error"]
    assert "subagents" not in final["policy"]
    assert all("subagent_run" not in _offered(request) for request in peer.requests)


def test_restored_report_evidence_must_match_the_selected_source_versions(tmp_path: Path) -> None:
    sha = hashlib.sha256(FACTS).hexdigest()
    text = "Revenue was 42 (facts.md)."
    result = {"evidence_type": "subagent_report", "path": "subagent", "task": "What was the revenue?", "model": "m",
              "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(), "line_start": 1, "line_end": 1,
              "sources": [{"path": "facts.md", "sha256": sha}], "steps": 2, "evidence_id": "E1"}
    assert subagent.is_report_evidence(result)
    assert not subagent.is_report_evidence({**result, "sha256": "0" * 64})
    assert not subagent.is_report_evidence({**result, "sources": []})

    def tools(source_sha: str) -> Tools:
        return Tools(None, "run", tmp_path, [{"path": "facts.md", "sha256": source_sha, "bytes": len(FACTS)}],  # type: ignore[arg-type]
                     subagents=True)

    confirmed = [{"tool": "subagent_run", "result": result}]
    restored = tools(sha)
    restored.restore_evidence(confirmed)
    assert restored.evidence["E1"]["text"] == text
    with pytest.raises(RunFailure) as raised:
        tools("f" * 64).restore_evidence(confirmed)
    assert raised.value.code == "checkpoint_evidence_invalid"


@pytest.mark.parametrize(("report", "expected"), [
    ("Revenue was 42 [facts.md E1].", "Revenue was 42 (facts.md)."),
    ("Total 6563 [sales.xlsx E2]; ratio 12.5% [E3].", "Total 6563 (sales.xlsx); ratio 12.5%."),
    ("No citation here.", "No citation here."),
])
def test_child_evidence_ids_are_rewritten_out_of_the_report(report: str, expected: str) -> None:
    assert subagent.without_child_ids(report) == expected


def test_a_rewritten_citation_names_the_real_source_not_the_label_the_child_wrote() -> None:
    """A small model may copy the prompt's placeholder label; the evidence record knows the real path."""
    sources = {"E1": ["vendor-a.md"], "E2": ["orders.csv"], "E3": ["orders.csv", "rates.csv"]}
    assert subagent.without_child_ids("單價 1850 元 [來源 E1]，交期 21 天 [來源 E1]。", sources) == \
        "單價 1850 元 (vendor-a.md)，交期 21 天 (vendor-a.md)。"
    assert subagent.without_child_ids("Total 9 [E2]; rate 3% [calc E3]; other [x E9].", sources) == \
        "Total 9 (orders.csv); rate 3% (orders.csv, rates.csv); other (x)."


def test_hooks_see_the_childs_tool_calls_too(tmp_path: Path) -> None:
    """A user's allow/deny policy cannot be bypassed by delegating: a deny refuses the child's call."""
    seen: list[dict] = []

    def hook(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if payload["tool"] == "source_read" and payload["arguments"]["path"] == "notes.md":
            return httpx.Response(200, json={"decision": "deny", "reason": "notes.md is off limits."})
        return httpx.Response(200, json={"decision": "allow"})

    config = tmp_path / "hooks.json"
    config.write_text(json.dumps({"hooks": [{"event": "before_tool", "url": "http://127.0.0.1:9/check",
                                             "tools": ["source_read"]}]}), encoding="utf-8")
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate", "Answer"]}, "plan"),
        tool_reply("subagent_run", {"task": "Revenue and budget?", "sources": ["facts.md", "notes.md"]}, "delegate"),
        tool_reply("source_read", {"path": "notes.md"}, "child-notes"),
        tool_reply("source_read", {"path": "facts.md"}, "child-facts"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]; the budget was not available."}, "child-finish"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [subagent E1]."}, "finish"),
    ], hooks_config=config, hooks_transport=httpx.MockTransport(hook))
    with app as client:
        final, events = finish(client, _start(client, tmp_path, completion_workflow="answer").json())

    assert final["status"] == "completed", final["error"]
    assert [(payload["call_id"], payload["arguments"]["path"]) for payload in seen] == [
        ("delegate/child-notes", "notes.md"), ("delegate/child-facts", "facts.md")]
    refused = _kinds(events, "subagent.tool_refused")
    assert [(data["child_call_id"], data["code"]) for data in refused] == [("child-notes", "hook_denied")]
    assert _report(events)["sources"] == [{"path": "facts.md", "sha256": hashlib.sha256(FACTS).hexdigest()}]
