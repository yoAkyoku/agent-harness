"""Bring recorded model drafts up to the current body-citation contract.

Some fixtures were recorded before the server rejected bare evidence IDs in
artifact bodies. Tests that replay them to check context handling (not
citation validation) rewrite each bare ``E<n>`` in a bracketed form the server
accepts, so the replay exercises the behaviour under test. Rewriting changes
draft bytes, so replays publish the draft SHA the server actually returned.
"""

import hashlib
import json
import re
from typing import Any

import httpx

_BARE_EID = re.compile(r"(?<![\[\w])E(\d+)\b(?!\])")


def bracket_bare_eids(content: str, path: str | None = None) -> str:
    """``E2`` -> ``[path E2]`` (or ``[E2]`` when the evidence may not be a source file).

    Only the model-written body is rewritten; the server-owned evidence
    appendix keeps its own format.
    """
    prefix = f"{path} " if path else ""
    body, marker, appendix = content.partition("\n\n## Verified tool evidence")
    return _BARE_EID.sub(lambda match: f"[{prefix}E{match[1]}]", body) + marker + appendix


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def bracket_draft_calls(wire_content: str, path: str | None = None) -> str:
    """Rewrite artifact_draft/artifact_write content inside a JSON-tools decision."""
    decision = json.loads(wire_content)
    if not isinstance(decision, dict):
        return wire_content
    for call in decision.get("calls", []):
        arguments: dict[str, Any] = call.get("arguments", {})
        if call.get("name") in {"artifact_draft", "artifact_write"} and isinstance(arguments.get("content"), str):
            arguments["content"] = bracket_bare_eids(arguments["content"], path)
    return json.dumps(decision, ensure_ascii=False, separators=(",", ":"))


def _draft_receipts(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("name") == "artifact_draft" and isinstance(node.get("value"), dict) \
                and isinstance(node["value"].get("sha256"), str):
            found.append(node["value"]["sha256"])
        for value in node.values():
            found.extend(_draft_receipts(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_draft_receipts(value))
    return found


def latest_draft_sha(request_body: dict[str, Any]) -> str | None:
    """SHA of the newest artifact_draft receipt visible in a JSON-tools request."""
    receipts: list[str] = []
    for message in request_body.get("messages", []):
        try:
            receipts.extend(_draft_receipts(json.loads(message.get("content") or "")))
        except (TypeError, ValueError):
            continue
    return receipts[-1] if receipts else None


def publish_current_draft(response: httpx.Response, request_body: dict[str, Any]) -> httpx.Response:
    """Point a replayed draft_sha256 publication at the draft the server holds now."""
    try:
        wire = response.json()["choices"][0]["message"]["content"]
        decision = json.loads(wire)
    except (KeyError, TypeError, ValueError):
        return response
    if not isinstance(decision, dict):
        return response
    current = latest_draft_sha(request_body)
    changed = False
    for call in decision.get("calls", []):
        arguments = call.get("arguments", {})
        if call.get("name") == "artifact_write" and "draft_sha256" in arguments and current:
            changed = changed or arguments["draft_sha256"] != current
            arguments["draft_sha256"] = current
    if not changed:
        return response
    payload = response.json()
    payload["choices"][0]["message"]["content"] = json.dumps(decision, ensure_ascii=False, separators=(",", ":"))
    return httpx.Response(response.status_code, json=payload)
