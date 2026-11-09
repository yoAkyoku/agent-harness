"""Hooks: user-configured HTTP webhooks before and after tool calls. Shell commands are not supported.

`--hooks-config` names a file with {"hooks": [...]}. Each hook has an `event`, a `url` (HTTPS, or
HTTP to loopback), optional `tools` name patterns (default all), optional credentials named by
environment variable (`bearerTokenEnv`, `headerEnv`) and a `timeoutSeconds` (default 5, at most 30).

- `before_tool` receives the call (tool, arguments, effect) and answers {"decision": "allow"|"deny",
  "reason": "..."}. Any deny refuses the call, and the reason goes back to the model. A hook that
  cannot be reached, times out, or answers anything else counts as a deny: it fails closed.
- `after_tool` receives the call and a summary of its result (no source text); it is observe-only,
  and a failed delivery is recorded without affecting the Run.

Hooks cannot change arguments, and verifiers never consult them.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path
from typing import Any

import httpx

from .remote import ENV_NAME, check_header_env, check_url, read_headers

Record = dict[str, Any]
EVENTS = ("before_tool", "after_tool")
MAX_REASON_CHARS = 500
SUMMARY_KEYS = ("evidence_id", "path", "name", "sha256", "bytes", "artifact_id", "version")


@dataclass(frozen=True)
class Hook:
    index: int
    event: str
    url: str
    tools: tuple[str, ...]
    timeout: float
    bearer_env: str | None
    header_env: dict[str, str]

    def matches(self, tool: str) -> bool:
        return any(fnmatchcase(tool, pattern) for pattern in self.tools)

    def public(self) -> Record:
        return {"event": self.event, "url": self.url, "tools": list(self.tools)}


def load_hooks(path: Path) -> list[Hook]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read the hooks config {path}: {error}") from error
    entries = value.get("hooks") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise ValueError("The hooks config needs a list named hooks.")
    hooks: list[Hook] = []
    for index, entry in enumerate(entries):
        owner = f"Hook {index + 1}"
        if not isinstance(entry, dict):
            raise ValueError(f"{owner}: expected an object.")
        if "command" in entry:
            raise ValueError(f"{owner}: hooks are HTTP webhooks; shell commands are not supported.")
        unknown = set(entry) - {"event", "url", "tools", "timeoutSeconds", "bearerTokenEnv", "headerEnv"}
        if unknown:
            raise ValueError(f"{owner}: unknown keys {sorted(unknown)}; credentials are read from environment "
                             "variables named by bearerTokenEnv or headerEnv.")
        if entry.get("event") not in EVENTS:
            raise ValueError(f"{owner}: event must be one of {', '.join(EVENTS)}.")
        if "url" not in entry:
            raise ValueError(f"{owner}: a url is required.")
        url = check_url(owner, entry["url"])
        tools = entry.get("tools", ["*"])
        if not isinstance(tools, list) or not tools or not all(isinstance(item, str) and item for item in tools):
            raise ValueError(f"{owner}: tools must be a non-empty list of tool names or patterns such as mcp__*.")
        timeout = entry.get("timeoutSeconds", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError(f"{owner}: timeoutSeconds must be more than 0 and at most 30.")
        bearer = entry.get("bearerTokenEnv")
        if bearer is not None and (not isinstance(bearer, str) or not ENV_NAME.fullmatch(bearer)):
            raise ValueError(f"{owner}: bearerTokenEnv must name an environment variable.")
        header_env = check_header_env(owner, entry.get("headerEnv", {}))
        hooks.append(Hook(index + 1, entry["event"], url, tuple(tools), float(timeout), bearer, header_env))
    return hooks


class HookRunner:
    def __init__(self, hooks: list[Hook], transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.hooks = hooks
        self._client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)

    @classmethod
    def from_config(cls, path: Path, transport: httpx.AsyncBaseTransport | None = None) -> HookRunner:
        return cls(load_hooks(path), transport)

    async def close(self) -> None:
        await self._client.aclose()

    def status(self) -> list[Record]:
        return [hook.public() for hook in self.hooks]

    async def _post(self, hook: Hook, payload: Record) -> httpx.Response:
        headers = read_headers(hook.header_env, bearer_env=hook.bearer_env)
        return await self._client.post(hook.url, json=payload, headers=headers, timeout=hook.timeout)

    async def before_tool(self, run_id: str, call_id: str, tool: str, arguments: Record, effect: str) -> list[Record]:
        """One decision per matching hook, in config order; the caller refuses the call on any deny."""
        payload = {"event": "before_tool", "run_id": run_id, "call_id": call_id, "tool": tool,
                   "arguments": arguments, "effect": effect}
        decisions: list[Record] = []
        for hook in self.hooks:
            if hook.event != "before_tool" or not hook.matches(tool):
                continue
            decision, reason = "deny", ""
            try:
                response = await self._post(hook, payload)
                answer = response.json() if response.status_code == 200 else None
                if isinstance(answer, dict) and answer.get("decision") in {"allow", "deny"}:
                    decision = answer["decision"]
                    reason = str(answer.get("reason") or "")[:MAX_REASON_CHARS]
                else:
                    reason = f"hook {hook.index} did not answer with a decision (HTTP {response.status_code})"
            except (httpx.HTTPError, ValueError, RuntimeError) as error:  # unreachable, timed out, not JSON, no credential
                reason = f"hook {hook.index} is unavailable: {type(error).__name__}: {error}"[:MAX_REASON_CHARS]
            decisions.append({"hook": hook.index, "event": "before_tool", "call_id": call_id, "tool": tool,
                              "decision": decision, **({"reason": reason} if reason else {})})
        return decisions

    async def after_tool(self, run_id: str, call_id: str, tool: str, arguments: Record, result: Record) -> list[Record]:
        summary = {key: result[key] for key in SUMMARY_KEYS if key in result}
        payload = {"event": "after_tool", "run_id": run_id, "call_id": call_id, "tool": tool,
                   "arguments": arguments, "result": summary}
        notices: list[Record] = []
        for hook in self.hooks:
            if hook.event != "after_tool" or not hook.matches(tool):
                continue
            notice: Record = {"hook": hook.index, "event": "after_tool", "call_id": call_id, "tool": tool}
            try:
                response = await self._post(hook, payload)
                if response.is_success:
                    notice["status"] = "delivered"
                else:
                    notice.update(status="failed", error=f"HTTP {response.status_code}")
            except (httpx.HTTPError, RuntimeError) as error:
                notice.update(status="failed", error=f"{type(error).__name__}: {error}"[:MAX_REASON_CHARS])
            notices.append(notice)
        return notices
