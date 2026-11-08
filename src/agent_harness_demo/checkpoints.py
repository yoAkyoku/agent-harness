"""A recovery checkpoint contains verified pairs, never invented tool results."""

from copy import deepcopy
import json
from typing import Any

from .context import completed_tool_group, history_turn, published_draft_group

Record = dict[str, Any]


def _omit_published_drafts(messages: list[Record]) -> None:
    # Mixed read/draft groups stay intact here; Runtime can retain their reads
    # in executor state when it later prunes an acknowledged published group.
    while (draft := published_draft_group(messages, 0, len(messages), set())) is not None:
        first, after, _ = draft
        del messages[first:after]


def resume_blockers(checkpoint: Record) -> list[Record]:
    blockers: list[Record] = deepcopy(checkpoint["blockers"])
    for call in checkpoint["confirmed"]:
        blocker = {"call_id": call["call_id"], "code": "incomplete_saved_intent"}
        if (not isinstance(call.get("arguments"), dict) or not isinstance(call.get("scope"), dict)) and blocker not in blockers:
            blockers.append(blocker)
    return blockers


def checkpoint_payload(run: Record, events: list[Record], artifacts: list[Record]) -> Record:
    policy = json.loads(run["policy_json"])
    ancestor = json.loads(run.get("resume_json") or "null") or {}
    pending: dict[str, Record] = {}
    new_decision = any(event["kind"] == "model.responded" for event in events)
    for attempt in ancestor.get("pending", []):
        if not new_decision or attempt.get("error", {}).get("retryable"):
            key = f"recovery-{ancestor['source_run_id']}-{attempt['call_id']}"
            pending[key] = {**deepcopy(attempt), "call_id": key}
    confirmed: list[Record] = deepcopy(ancestor.get("confirmed", []))
    seen_calls: set[str] = set()
    protocol_blockers: list[Record] = []
    for event in events:
        kind, data = event["kind"], event["data"]
        if kind == "model.responded":
            for call in data.get("message", {}).get("tool_calls", []):
                identity = call["id"]
                if identity in seen_calls:
                    protocol_blockers.append({"call_id": identity, "code": "ambiguous_call_identity"})
                    continue
                seen_calls.add(identity)
                try:
                    arguments = call["function"]["arguments"]
                    arguments = json.loads(arguments) if isinstance(arguments, str) else deepcopy(arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError
                except (ValueError, TypeError):
                    protocol_blockers.append({"call_id": identity, "code": "invalid_saved_call"})
                    continue
                pending[identity] = {"call_id": identity, "tool": call["function"]["name"],
                                     "arguments": arguments, "status": "not_dispatched"}
        elif kind == "tool.requested":
            pending[data["call_id"]] = {**deepcopy(data), "status": "not_dispatched"}
        elif kind == "tool.dispatched":
            pending[data["call_id"]] = {**deepcopy(data), "status": "unknown"}
        elif kind in {"tool.completed", "tool.reconciled", "tool.reused"}:
            intent = pending.pop(data["call_id"], None)
            if intent is not None:
                record = {**intent, **deepcopy(data), "origin_run_id": run["id"], "status": "confirmed", "result": deepcopy(data["result"])}
                if (kind != "tool.reused" or data["tool"] in {"plan_update", "artifact_draft"}
                        or (data["tool"] == "artifact_write" and policy.get("publication_workflow") == "draft_review")):
                    confirmed.append(record)
        elif kind in {"tool.failed", "tool.rejected"} and data["call_id"] in pending:
            pending[data["call_id"]]["status"] = "failed" if kind == "tool.failed" else "rejected"
        elif kind == "correction.available" and data["call_id"] in pending:
            pending[data["call_id"]]["error"] = deepcopy(data["error"])
            for identity in data.get("superseded_call_ids", []):
                pending.pop(identity, None)
        elif kind == "correction.observed":
            for identity in data["failed_call_ids"]:
                pending.pop(identity, None)

    raw = history_turn(run["id"], run["task"], events)[1:]
    messages: list[Record] = deepcopy(ancestor.get("messages", []))
    index = 0
    while index < len(raw):
        assistant = raw[index]
        index += 1
        if assistant["role"] != "assistant":
            continue
        results: dict[str, Record] = {}
        while index < len(raw) and raw[index]["role"] == "tool":
            results[raw[index]["tool_call_id"]] = raw[index]
            index += 1
        if not assistant.get("tool_calls"):
            messages.append(assistant)
            continue
        calls = [call for call in assistant["tool_calls"] if call["id"] in results]
        if calls:
            messages.append({**assistant, "tool_calls": calls})
            messages.extend(results[call["id"]] for call in calls)

    resolved = {f"h-{run['id']}-{identity}" for event in events if event["kind"] == "correction.observed"
                for identity in event["data"]["failed_call_ids"]}
    error_tools = {call["function"]["name"] for message in messages for call in message.get("tool_calls", [])
                   if call["id"] in resolved}
    while (obsolete := completed_tool_group(messages, 0, len(messages), error_tools, resolved_error_ids=resolved)) is not None:
        first, after, _ = obsolete
        del messages[first:after]

    # Planning state is restored from confirmed chronology. Keep its latest full
    # pair, rather than replaying superseded plans into the first resumed request.
    while (older := completed_tool_group(messages, 0, len(messages), {"plan_update"})) is not None:
        first, after, _ = older
        if completed_tool_group(messages, after, len(messages), {"plan_update"}) is None:
            break
        del messages[first:after]
    _omit_published_drafts(messages)

    blockers = protocol_blockers + [{"call_id": call["call_id"], "code": "unknown_effect", "effect": call.get("effect", "unknown")}
                for call in pending.values() if call["status"] == "unknown" and call.get("effect") not in {"read_only", "run_metadata"}]
    blockers = resume_blockers({"blockers": blockers, "confirmed": confirmed})
    return {"version": 1, "source_run_id": run["id"], "task": run["task"],
            "sources": json.loads(run["sources_json"]), "limits": json.loads(run["limits_json"]),
            "model": json.loads(run["model_json"]), "policy": policy,
            "confirmed": confirmed, "pending": list(pending.values()), "messages": messages,
            "artifacts": list({item["id"]: item for item in [*ancestor.get("artifacts", []), *artifacts]}.values()), "blockers": blockers}
