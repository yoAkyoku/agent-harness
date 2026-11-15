"""A Run's events as an OpenTelemetry trace (OTLP/JSON) at GET /api/runs/{id}/trace.

The trace is derived from the saved events, so it exists for any Run and never changes one. The Run
is an `invoke_agent` span; model requests are `chat` spans with token usage; tool calls are
`execute_tool` spans; a subagent is a nested `invoke_agent` span under the tool call that started it.
Following the GenAI conventions' default, the trace carries no prompt, source or result text.
"""
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.trace import run_trace
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply


def _attributes(span: dict) -> dict:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def _run(tmp_path: Path, replies: list[httpx.Response], **body: object) -> tuple[dict, dict, list[dict]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\nOwner: Mei\n")
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(CompletionPeer(replies)),
                     allow_subagents=True)
    with TestClient(app) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs",
                          json={"task": "What was the revenue?", "sources": ["facts.md"], **body}).json()
        final, events = finish(client, run)
        response = client.get(f"/api/runs/{final['id']}/trace")
        assert response.status_code == 200, response.text
        assert client.get("/api/runs/missing/trace").status_code == 404
    return final, response.json(), events


def test_a_run_is_exported_as_one_trace_of_agent_model_and_tool_spans(tmp_path: Path) -> None:
    final, trace, events = _run(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "Revenue was 42."}, "uncited"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]."}, "finish"),
    ], completion_workflow="answer")

    assert final["status"] == "completed", final["error"]
    resource = trace["resourceSpans"][0]
    assert _attributes(resource["resource"]) == {"service.name": "agent-harness"}
    spans = resource["scopeSpans"][0]["spans"]
    assert len({span["traceId"] for span in spans}) == 1 and len(spans[0]["traceId"]) == 32
    assert len({span["spanId"] for span in spans}) == len(spans) and all(len(span["spanId"]) == 16 for span in spans)
    root = spans[0]
    assert root["name"] == "invoke_agent agent-harness" and "parentSpanId" not in root and root["status"] == {"code": 1}
    attributes = _attributes(root)
    assert attributes["gen_ai.operation.name"] == "invoke_agent" and attributes["gen_ai.request.model"] == "contract-model"
    assert attributes["agent_harness.run.id"] == final["id"] and attributes["agent_harness.run.status"] == "completed"
    assert attributes["gen_ai.conversation.id"] == final["session_id"]

    chats = [span for span in spans if span["name"] == "chat contract-model"]
    assert len(chats) == 4 and all(span["parentSpanId"] == root["spanId"] and span["kind"] == 3 for span in chats)
    assert [_attributes(span)["agent_harness.step"] for span in chats] == ["1", "2", "3", "4"]
    tools = [span for span in spans if span["name"].startswith("execute_tool ")]
    assert [span["name"] for span in tools] == ["execute_tool plan_update", "execute_tool source_read",
                                                "execute_tool task_finish", "execute_tool task_finish"]
    read, rejected = tools[1], tools[2]
    assert _attributes(read)["gen_ai.tool.call.id"] == "read" and _attributes(read)["agent_harness.evidence_id"] == "E1"
    assert rejected["status"] == {"code": 2, "message": "missing_answer_citation"}
    assert _attributes(rejected)["error.type"] == "missing_answer_citation"
    assert all(int(span["startTimeUnixNano"]) <= int(span["endTimeUnixNano"]) for span in spans)
    assert "correction.available" in [event["name"] for event in root["events"]]

    # No content: nothing from the task, the source or the answer is in the trace.
    text = json.dumps(trace)
    assert "Revenue" not in text and "Mei" not in text and "What was" not in text


def test_a_subagent_is_a_nested_agent_span_under_its_tool_call(tmp_path: Path) -> None:
    final, trace, _ = _run(tmp_path, [
        tool_reply("plan_update", {"steps": ["Delegate", "Answer"]}, "plan"),
        tool_reply("subagent_run", {"task": "What was the revenue?", "sources": ["facts.md"]}, "delegate"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]."}, "child-finish"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [subagent E1]."}, "finish"),
    ], completion_workflow="answer", subagents=True)

    assert final["status"] == "completed", final["error"]
    spans = trace["resourceSpans"][0]["scopeSpans"][0]["spans"]
    by_id = {span["spanId"]: span for span in spans}
    delegate = next(span for span in spans if span["name"] == "execute_tool subagent_run")
    child = next(span for span in spans if span["name"] == "invoke_agent subagent")
    assert child["parentSpanId"] == delegate["spanId"] and child["status"] == {"code": 1}
    assert _attributes(child)["agent_harness.subagent.steps"] == "2"
    inside = [span["name"] for span in spans if span.get("parentSpanId") == child["spanId"]]
    assert inside == ["chat contract-model", "execute_tool source_read", "chat contract-model"]
    assert int(delegate["startTimeUnixNano"]) <= int(child["startTimeUnixNano"]) <= int(child["endTimeUnixNano"]) <= int(delegate["endTimeUnixNano"])
    assert all(span.get("parentSpanId") is None or span["parentSpanId"] in by_id for span in spans)


def test_an_unfinished_run_has_open_work_marked_and_no_outcome() -> None:
    run = {"id": "r1", "session_id": "s1", "mode": "local", "status": "running", "model": {"name": "m"}}
    at = "2026-11-16T00:00:0{}+00:00"
    events = [
        {"sequence": 1, "kind": "run.accepted", "data": {}, "timestamp": at.format(0)},
        {"sequence": 2, "kind": "model.requested", "data": {"step": 1}, "timestamp": at.format(1)},
        {"sequence": 3, "kind": "model.responded", "data": {"step": 1, "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
         "timestamp": at.format(2)},
        {"sequence": 4, "kind": "tool.requested", "data": {"call_id": "c1", "tool": "source_read"}, "timestamp": at.format(3)},
    ]
    spans = run_trace(run, events)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root, chat, tool = spans
    assert root["status"] == {} and _attributes(root)["agent_harness.run.status"] == "running"
    assert _attributes(chat)["gen_ai.usage.input_tokens"] == "10" and _attributes(chat)["gen_ai.usage.output_tokens"] == "2"
    assert tool["status"] == {"code": 2, "message": "unfinished"}
    assert run_trace(run, []) == {"resourceSpans": []}
