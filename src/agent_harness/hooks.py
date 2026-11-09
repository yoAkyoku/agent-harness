"""Hooks: user-configured checks before and after tool calls.

`--hooks-config` names a file with {"hooks": [...]}. Each hook has an `event`, optional `tools` name
patterns (default all) and a `timeoutSeconds` (default 5, at most 30), and is either:

- a webhook: a `url` (HTTPS, or HTTP to loopback), with credentials named by environment variable
  (`bearerTokenEnv`, `headerEnv`); or
- a command hook: `command`, a list of program arguments started directly, never through a shell,
  allowed only when the workbench starts with --allow-command-hooks. The payload arrives as JSON on
  stdin and the answer is read from stdout. The program gets a minimal environment plus only the
  variables named in `passEnv`, and runs in the folder of the hooks config file.

A `before_tool` hook receives the call (tool, arguments, effect) and answers
{"decision": "allow"|"deny", "reason": "..."}. Any deny refuses the call, and the reason goes back
to the model. A hook that cannot be reached, fails, times out or answers anything else counts as a
deny: it fails closed. An `after_tool` hook receives the call and a summary of its result (no
source text). It is observe-only, and a failed delivery is recorded without affecting the Run.

Hooks cannot change arguments, and verifiers never consult them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
from typing import Any

import httpx

from .remote import ENV_NAME, check_header_env, check_url, read_headers

Record = dict[str, Any]
EVENTS = ("before_tool", "after_tool")
MAX_REASON_CHARS = 500
MAX_OUTPUT_BYTES = 65_536
SUMMARY_KEYS = ("evidence_id", "path", "name", "sha256", "bytes", "artifact_id", "version")
# What a command hook inherits: enough to find and start programs, nothing else unless passEnv names it.
BASE_ENVIRONMENT = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")


class HookFailure(Exception):
    """A hook that did not answer properly; its message becomes the deny reason or the failure note."""


@dataclass(frozen=True)
class Hook:
    index: int
    event: str
    tools: tuple[str, ...]
    timeout: float
    url: str | None = None
    bearer_env: str | None = None
    header_env: tuple[tuple[str, str], ...] = ()
    command: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()
    cwd: str | None = None

    def matches(self, tool: str) -> bool:
        return any(fnmatchcase(tool, pattern) for pattern in self.tools)

    def public(self) -> Record:
        target: Record = {"url": self.url} if self.url is not None else {"command": list(self.command)}
        return {"event": self.event, **target, "tools": list(self.tools)}


def load_hooks(path: Path, *, allow_commands: bool = False) -> list[Hook]:
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
        if "command" in entry and "url" in entry:
            raise ValueError(f"{owner}: use either url (a webhook) or command (a local program), not both.")
        allowed_keys = ({"event", "command", "tools", "timeoutSeconds", "passEnv"} if "command" in entry
                        else {"event", "url", "tools", "timeoutSeconds", "bearerTokenEnv", "headerEnv"})
        unknown = set(entry) - allowed_keys
        if unknown:
            raise ValueError(f"{owner}: unknown keys {sorted(unknown)}; credentials are read from environment "
                             "variables (bearerTokenEnv or headerEnv for webhooks, passEnv for commands).")
        if entry.get("event") not in EVENTS:
            raise ValueError(f"{owner}: event must be one of {', '.join(EVENTS)}.")
        tools = entry.get("tools", ["*"])
        if not isinstance(tools, list) or not tools or not all(isinstance(item, str) and item for item in tools):
            raise ValueError(f"{owner}: tools must be a non-empty list of tool names or patterns such as mcp__*.")
        timeout = entry.get("timeoutSeconds", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError(f"{owner}: timeoutSeconds must be more than 0 and at most 30.")
        if "command" in entry:
            command = entry["command"]
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                raise ValueError(f"{owner}: command is a list of program arguments; it is never run through a shell.")
            if not allow_commands:
                raise ValueError(f"{owner}: command hooks run local programs; start the workbench with "
                                 "--allow-command-hooks to enable them.")
            pass_env = entry.get("passEnv", [])
            if not isinstance(pass_env, list) or not all(isinstance(item, str) and ENV_NAME.fullmatch(item) for item in pass_env):
                raise ValueError(f"{owner}: passEnv must list environment variable names.")
            hooks.append(Hook(index + 1, entry["event"], tuple(tools), float(timeout), command=tuple(command),
                              pass_env=tuple(pass_env), cwd=str(path.resolve().parent)))
            continue
        if "url" not in entry:
            raise ValueError(f"{owner}: a url (a webhook) or a command (a local program) is required.")
        url = check_url(owner, entry["url"])
        bearer = entry.get("bearerTokenEnv")
        if bearer is not None and (not isinstance(bearer, str) or not ENV_NAME.fullmatch(bearer)):
            raise ValueError(f"{owner}: bearerTokenEnv must name an environment variable.")
        header_env = check_header_env(owner, entry.get("headerEnv", {}))
        hooks.append(Hook(index + 1, entry["event"], tuple(tools), float(timeout), url=url, bearer_env=bearer,
                          header_env=tuple(header_env.items())))
    return hooks


class HookRunner:
    def __init__(self, hooks: list[Hook], transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.hooks = hooks
        self._client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)

    @classmethod
    def from_config(cls, path: Path, transport: httpx.AsyncBaseTransport | None = None, *,
                    allow_commands: bool = False) -> HookRunner:
        return cls(load_hooks(path, allow_commands=allow_commands), transport)

    async def close(self) -> None:
        await self._client.aclose()

    def status(self) -> list[Record]:
        return [hook.public() for hook in self.hooks]

    async def _deliver(self, hook: Hook, payload: Record, *, answer_expected: bool) -> Any:
        """Send the payload; return the parsed answer (before_tool) or None; raise HookFailure otherwise."""
        if hook.command:
            output = await self._run_command(hook, payload)
            if not answer_expected:
                return None
            try:
                return json.loads(output)
            except ValueError as error:
                raise HookFailure(f"hook {hook.index} did not print a JSON decision") from error
        try:
            headers = read_headers(dict(hook.header_env), bearer_env=hook.bearer_env)
            response = await self._client.post(str(hook.url), json=payload, headers=headers, timeout=hook.timeout)
        except (httpx.HTTPError, RuntimeError) as error:  # unreachable, timed out, or no credential
            raise HookFailure(f"hook {hook.index} is unavailable: {type(error).__name__}: {error}") from error
        if not answer_expected:
            if not response.is_success:
                raise HookFailure(f"HTTP {response.status_code}")
            return None
        if response.status_code != 200:
            raise HookFailure(f"hook {hook.index} did not answer with a decision (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError as error:
            raise HookFailure(f"hook {hook.index} did not answer with a decision (not JSON)") from error

    async def _run_command(self, hook: Hook, payload: Record) -> bytes:
        environment = {name: os.environ[name] for name in (*BASE_ENVIRONMENT, *hook.pass_env) if name in os.environ}
        try:
            process = await asyncio.create_subprocess_exec(
                *hook.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=environment, cwd=hook.cwd)
        except OSError as error:
            raise HookFailure(f"hook {hook.index} could not start: {type(error).__name__}: {error}") from error
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode("utf-8")), hook.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise HookFailure(f"hook {hook.index} timed out after {hook.timeout:g} s") from None
        if process.returncode != 0:
            raise HookFailure(f"hook {hook.index} exited with status {process.returncode}")
        if len(output) > MAX_OUTPUT_BYTES:
            raise HookFailure(f"hook {hook.index} printed more than {MAX_OUTPUT_BYTES} bytes")
        return output

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
                answer = await self._deliver(hook, payload, answer_expected=True)
                if isinstance(answer, dict) and answer.get("decision") in {"allow", "deny"}:
                    decision = answer["decision"]
                    reason = str(answer.get("reason") or "")
                else:
                    reason = f"hook {hook.index} did not answer with a decision"
            except HookFailure as failure:
                reason = str(failure)
            decisions.append({"hook": hook.index, "event": "before_tool", "call_id": call_id, "tool": tool,
                              "decision": decision, **({"reason": reason[:MAX_REASON_CHARS]} if reason else {})})
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
                await self._deliver(hook, payload, answer_expected=False)
                notice["status"] = "delivered"
            except HookFailure as failure:
                notice.update(status="failed", error=str(failure)[:MAX_REASON_CHARS])
            notices.append(notice)
        return notices
