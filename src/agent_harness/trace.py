"""A Run's saved events as an OpenTelemetry trace (OTLP/JSON), for any collector or trace viewer.

The event log is the source of truth; this module only re-reads it, so a trace can be produced for
any Run at any time and never changes one. Spans follow the OpenTelemetry GenAI semantic conventions
(still marked as in development there): the Run is an `invoke_agent` span, each model request a `chat`
span with token usage, each tool call an `execute_tool` span, and a subagent a nested `invoke_agent`
span under the tool call that started it. Context steps, corrections, approvals and hook decisions are
span events. Following the conventions' default, no prompt, source or result text is included: only
names, sizes, codes and hashes.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import httpx

Record = dict[str, Any]
INTERNAL, CLIENT = 1, 3  # SpanKind
OK, ERROR = 1, 2  # StatusCode
TOOL_END = {"tool.completed": None, "tool.reused": None, "tool.reconciled": None, "tool.failed": "failed", "tool.rejected": "rejected"}
RUN_END = {"run.completed": None, "run.failed": "failed", "run.cancelled": "cancelled", "run.interrupted": "interrupted"}
ROOT_EVENTS = ("context.pruned", "context.compacted", "context.compaction_failed", "context.limited", "correction.available",
               "correction.observed", "approval.requested", "approval.approved", "approval.denied", "approval.expired",
               "hook.decided", "message.delivered", "answer.verified", "artifact.published", "checkpoint.loaded")
EVENT_KEYS = ("reason", "code", "tool", "decision", "call_id", "evidence_id", "step", "chunks", "original_text_bytes",
              "summary_bytes", "upper_bound_tokens", "capacity_tokens", "name", "sha256")


def _nanos(timestamp: str) -> str:
    return str(int(datetime.fromisoformat(timestamp).timestamp() * 1_000_000_000))


def _attribute(key: str, value: Any) -> Record:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}  # 64-bit integers are strings in OTLP/JSON
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _attributes(values: Record) -> list[Record]:
    return [_attribute(key, value) for key, value in values.items() if value is not None]


class _Trace:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.trace_id = hashlib.sha256(f"agent-harness-run:{run_id}".encode()).hexdigest()[:32]
        self.spans: list[Record] = []

    def span_id(self, key: str) -> str:
        return hashlib.sha256(f"{self.run_id}:{key}".encode()).hexdigest()[:16]

    def open(self, key: str, name: str, parent: str | None, start: str, kind: int, attributes: Record) -> Record:
        span = {"traceId": self.trace_id, "spanId": self.span_id(key), "name": name, "kind": kind,
                "startTimeUnixNano": _nanos(start), "endTimeUnixNano": None,
                "attributes": _attributes(attributes), "events": [], "status": {}}
        if parent is not None:
            span["parentSpanId"] = parent
        self.spans.append(span)
        return span

    @staticmethod
    def close(span: Record, end: str, error: str | None = None, attributes: Record | None = None) -> None:
        span["endTimeUnixNano"] = _nanos(end)
        span["attributes"] += _attributes(attributes or {})
        span["status"] = {"code": ERROR, "message": error} if error else {"code": OK}
        if error:
            span["attributes"].append(_attribute("error.type", error))

    @staticmethod
    def event(span: Record, name: str, at: str, data: Record) -> None:
        span["events"].append({"name": name, "timeUnixNano": _nanos(at),
                               "attributes": _attributes({f"agent_harness.{key}": data[key] for key in EVENT_KEYS
                                                          if isinstance(data.get(key), (str, int, float, bool))})})


def _usage(usage: Record | None) -> Record:
    usage = usage or {}
    return {"gen_ai.usage.input_tokens": usage.get("prompt_tokens"), "gen_ai.usage.output_tokens": usage.get("completion_tokens")}


def run_trace(run: Record, events: list[Record]) -> Record:
    """OTLP/JSON `resourceSpans` for one Run, built from its events in order."""
    trace = _Trace(run["id"])
    model = run.get("model", {}).get("name", "unknown")
    if not events:
        return {"resourceSpans": []}
    first, last = events[0]["timestamp"], events[-1]["timestamp"]
    root = trace.open("run", "invoke_agent agent-harness", None, first, INTERNAL, {
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "agent-harness", "gen_ai.request.model": model,
        "gen_ai.conversation.id": run.get("session_id"), "agent_harness.run.id": run["id"],
        "agent_harness.run.mode": run.get("mode")})
    chats: dict[tuple[str, int], Record] = {}  # (owner, step) -> open chat span; owner "" is the Run itself
    tools: dict[str, Record] = {}
    subagents: dict[str, Record] = {}
    ended: str | None = None
    for event in events:
        kind, data, at = event["kind"], event["data"], event["timestamp"]
        if kind in {"model.requested", "subagent.model_requested"}:
            owner = data.get("call_id", "") if kind.startswith("subagent.") else ""
            parent = subagents[owner]["spanId"] if owner in subagents else root["spanId"]
            chats[(owner, data["step"])] = trace.open(f"chat:{owner}:{data['step']}", f"chat {model}", parent, at, CLIENT, {
                "gen_ai.operation.name": "chat", "gen_ai.request.model": model, "agent_harness.step": data["step"],
                "agent_harness.context.upper_bound_tokens": (data.get("context_budget") or {}).get("upper_bound_tokens"),
                "agent_harness.request.sha256": data.get("request_sha256")})
        elif kind in {"model.responded", "subagent.model_responded"}:
            owner = data.get("call_id", "") if kind.startswith("subagent.") else ""
            chat = chats.pop((owner, data["step"]), None)
            if chat is not None:
                trace.close(chat, at, attributes={**_usage(data.get("usage")), "agent_harness.tool_calls": data.get("tool_count")})
        elif kind == "compaction.model_responded":
            chat = trace.open(f"summary:{data['evidence_id']}:{data['chunk']}", f"chat {model}", root["spanId"], at, CLIENT, {
                "gen_ai.operation.name": "chat", "gen_ai.request.model": model, "agent_harness.purpose": "context_summary",
                "agent_harness.evidence_id": data["evidence_id"], "agent_harness.chunk": data["chunk"]})
            trace.close(chat, at, attributes=_usage(data.get("usage")))  # only its completion is recorded
        elif kind == "tool.requested":
            tools[data["call_id"]] = trace.open(f"tool:{data['call_id']}", f"execute_tool {data['tool']}", root["spanId"], at,
                                                INTERNAL, {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": data["tool"],
                                                           "gen_ai.tool.call.id": data["call_id"]})
        elif kind in TOOL_END and data.get("call_id") in tools:
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            trace.close(tools.pop(data["call_id"]), at, data.get("code") if TOOL_END[kind] else None,
                        {"agent_harness.evidence_id": result.get("evidence_id"), "agent_harness.tool.outcome": kind.split(".")[1]})
        elif kind == "subagent.started":
            parent = tools[data["call_id"]]["spanId"] if data["call_id"] in tools else root["spanId"]
            subagents[data["call_id"]] = trace.open(f"subagent:{data['call_id']}", "invoke_agent subagent", parent, at, INTERNAL, {
                "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "subagent", "gen_ai.request.model": model,
                "agent_harness.subagent.sources": len(data.get("sources", []))})
        elif kind in {"subagent.completed", "subagent.failed"} and data.get("call_id") in subagents:
            trace.close(subagents.pop(data["call_id"]), at, data.get("reason") if kind.endswith("failed") else None,
                        {"agent_harness.subagent.steps": data.get("steps")})
        elif kind in {"subagent.tool_completed", "subagent.tool_refused"} and data.get("call_id") in subagents:
            child = trace.open(f"subtool:{data['call_id']}:{event['sequence']}", f"execute_tool {data['tool']}",
                               subagents[data["call_id"]]["spanId"], at, INTERNAL, {
                                   "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": data["tool"],
                                   "gen_ai.tool.call.id": data.get("child_call_id")})
            trace.close(child, at, data.get("code") if kind.endswith("refused") else None)  # only its outcome is recorded
        elif kind in ROOT_EVENTS:
            target = tools.get(data.get("call_id", ""), root) if kind.startswith(("approval.", "hook.")) else root
            trace.event(target, kind, at, data)
        elif kind in RUN_END:
            ended = kind
            error = (data.get("code") or RUN_END[kind]) if RUN_END[kind] else None
            trace.close(root, at, error, {"agent_harness.run.status": kind.split(".")[1],
                                          **{f"gen_ai.usage.{name}": run.get("token_usage", {}).get(name)
                                             for name in ("input_tokens", "output_tokens")}})
    for span in [*chats.values(), *tools.values(), *subagents.values()]:
        trace.close(span, last, "unfinished")  # the Run ended, or is still running, before this step finished
    if ended is None:
        trace.close(root, last, None, {"agent_harness.run.status": run.get("status")})
        root["status"] = {}  # still running: no outcome yet
    return {"resourceSpans": [{
        "resource": {"attributes": _attributes({"service.name": "agent-harness"})},
        "scopeSpans": [{"scope": {"name": "agent_harness.trace"}, "spans": trace.spans}],
    }]}


class TraceExporter:
    """Pushes finished Runs' traces to one OTLP/HTTP endpoint; a failed push is counted and changes nothing else."""

    def __init__(self, endpoint: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        from .model import _validated_base_url  # https, or plain http on this computer; no credentials or query

        self.endpoint = _validated_base_url(endpoint)
        self.exported = self.failed = 0
        self._client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=10)

    def status(self) -> Record:
        return {"enabled": True, "endpoint": self.endpoint, "exported": self.exported, "failed": self.failed}

    async def export(self, run: Record, events: list[Record]) -> None:
        try:
            response = await self._client.post(self.endpoint, json=run_trace(run, events))
            response.raise_for_status()
        except httpx.HTTPError:
            self.failed += 1
            return
        self.exported += 1

    async def close(self) -> None:
        await self._client.aclose()
