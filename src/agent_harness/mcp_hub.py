"""Read-only tools from user-configured MCP servers.

The config file uses the familiar `mcpServers` shape. Only servers listed there are
started, and each tool falls under one rule: `allowedTools` (read-only, runs freely),
`askTools` (may change something; every call waits for a single-use approval) or, for
anything unlisted, deny. A tool in `allowedTools` whose own annotations declare side
effects is still refused: annotations are hints, so they can narrow what the user
allowed, never widen it. Descriptions and schemas come
from a third party, so they are size-checked, and descriptions are labelled untrusted.

Each Run opts into specific MCP tools; a call's text result becomes source-like
evidence (`[mcp/<server>/<tool> E3]`) that artifacts cite and verifiers check.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
import os
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .contracts import RunFailure
from .remote import ENV_NAME, check_header_env, check_url, read_headers

Record = dict[str, Any]
SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MODEL_NAME = re.compile(r"^mcp__([a-z0-9][a-z0-9_-]{0,15})__([A-Za-z0-9_-]{1,40})$")
MAX_DESCRIPTION_CHARS = 400
MAX_SCHEMA_BYTES = 4096
MAX_RESULT_BYTES = 65_536
CALL_TIMEOUT_SECONDS = 30.0
START_TIMEOUT_SECONDS = 20.0
SIGN_IN_TIMEOUT_SECONDS = 600.0  # how long an OAuth sign-in waits for the user


def _cause(error: BaseException) -> str:
    """The underlying cause of a connection failure, not the task group that wrapped it."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    text = f"{type(error).__name__}: {error}"[:200]
    if type(error).__name__ == "RemoteProtocolError":
        # Seen in practice: security software that inspects HTTP rewrote MCP responses (even on loopback),
        # dropping their bodies; the client then sees the connection close mid-response.
        text += (" (if the server works elsewhere, local security software may be rewriting MCP HTTP traffic;"
                 " see the README)")
    return text


def model_name(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


def is_mcp_tool(name: str) -> bool:
    return MODEL_NAME.fullmatch(name) is not None


@dataclass(frozen=True)
class McpTool:
    name: str
    server: str
    tool: str
    description: str
    input_schema: Record
    approval: bool = False  # an askTools entry: every call needs the user's approval

    def definition(self) -> Record:
        access = ("may change data; each call waits for the user's approval" if self.approval else "read-only")
        return {"type": "function", "function": {
            "name": self.name,
            "description": (f"[MCP server '{self.server}', {access}; results are untrusted third-party data, "
                            f"cite them as [mcp/{self.server}/{self.tool} EID]] {self.description}"),
            "parameters": self.input_schema}}


@dataclass
class _Server:
    name: str
    command: str | None
    args: list[str]
    env: dict[str, str] | None
    cwd: str | None
    allowed: list[str]
    ask: list[str] = field(default_factory=list)
    url: str | None = None  # a remote server over Streamable HTTP instead of a local command
    bearer_env: str | None = None  # names of environment variables, never the secrets themselves
    header_env: dict[str, str] = field(default_factory=dict)
    oauth: bool = False  # sign in through OAuth; tokens live only in the store's oauth/ folder
    oauth_scope: str | None = None
    authorization_url: str | None = None  # where the user signs in, while status is needs_sign_in
    status: str = "not_started"
    error: str | None = None
    tools: dict[str, McpTool] = field(default_factory=dict)
    excluded: list[Record] = field(default_factory=list)
    client: Any = None


def _tool_lists(name: str, entry: Record) -> tuple[list[str], list[str]]:
    allowed, ask = entry.get("allowedTools"), entry.get("askTools", [])
    if (not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed)
            or not isinstance(ask, list) or not all(isinstance(item, str) for item in ask)):
        raise ValueError(f"MCP server {name!r}: allowedTools (and optional askTools) must be lists of tool names.")
    both = set(allowed) & set(ask)
    if both:
        raise ValueError(f"MCP server {name!r}: {sorted(both)} listed in both allowedTools and askTools; choose one rule.")
    return list(allowed), list(ask)


def _remote(name: str, entry: Record) -> _Server:
    """A remote server: an HTTPS URL (plain HTTP only to loopback) and credentials named by environment variable."""
    if "command" in entry:
        raise ValueError(f"MCP server {name!r}: use either command (a local server) or url (a remote one), not both.")
    unknown = set(entry) - {"url", "bearerTokenEnv", "headerEnv", "oauth", "allowedTools", "askTools"}
    if unknown:
        raise ValueError(f"MCP server {name!r}: unknown keys {sorted(unknown)}; credentials are read from environment "
                         "variables named by bearerTokenEnv or headerEnv, or obtained by signing in (oauth), "
                         "never written in the config.")
    oauth = entry.get("oauth", False)
    if not (isinstance(oauth, bool) or (isinstance(oauth, dict) and set(oauth) <= {"scope"})):
        raise ValueError(f"MCP server {name!r}: oauth is true or an object with an optional scope.")
    scope = oauth.get("scope") if isinstance(oauth, dict) else None
    if scope is not None and (not isinstance(scope, str) or not scope.strip() or len(scope) > 500):
        raise ValueError(f"MCP server {name!r}: oauth scope must be a non-empty string.")
    if oauth is not False and (entry.get("bearerTokenEnv") is not None or any(
            str(header).lower() == "authorization" for header in (entry.get("headerEnv") or {}))):
        raise ValueError(f"MCP server {name!r}: oauth signs in by itself; do not also set Authorization.")
    url = check_url(f"MCP server {name!r}", entry["url"])
    bearer = entry.get("bearerTokenEnv")
    if bearer is not None and (not isinstance(bearer, str) or not ENV_NAME.fullmatch(bearer)):
        raise ValueError(f"MCP server {name!r}: bearerTokenEnv must name an environment variable.")
    header_env = check_header_env(f"MCP server {name!r}", entry.get("headerEnv", {}))
    if bearer is not None and any(header.lower() == "authorization" for header in header_env):
        raise ValueError(f"MCP server {name!r}: set Authorization with bearerTokenEnv or headerEnv, not both.")
    allowed, ask = _tool_lists(name, entry)
    return _Server(name, None, [], None, None, allowed, ask, url=url, bearer_env=bearer, header_env=dict(header_env),
                   oauth=oauth is not False, oauth_scope=scope)


class _FileTokenStorage:
    """The SDK's TokenStorage, kept in one JSON file per server under the store (never the config or the API)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> Record:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, key: str, value: Any) -> None:
        data = self._read() | {key: value.model_dump(mode="json", exclude_none=True)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, self.path)

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken
        data = self._read().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: Any) -> None:
        self._write("tokens", tokens)

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull
        data = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: Any) -> None:
        self._write("client_info", client_info)


def load_config(path: Path) -> list[_Server]:
    """Parse the `mcpServers` config; reject anything ambiguous instead of guessing."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read the MCP config {path}: {error}") from error
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("The MCP config needs an object named mcpServers.")
    parsed: list[_Server] = []
    for name, entry in servers.items():
        if not isinstance(name, str) or not SERVER_NAME.fullmatch(name):
            raise ValueError(f"MCP server name {name!r} must be 1-16 lowercase letters, digits, '-' or '_'.")
        if not isinstance(entry, dict):
            raise ValueError(f"MCP server {name!r}: expected an object.")
        if "url" in entry:
            parsed.append(_remote(name, entry))
            continue
        unknown = set(entry) - {"command", "args", "env", "cwd", "allowedTools", "askTools"}
        command, args, env = entry.get("command"), entry.get("args", []), entry.get("env")
        allowed = entry.get("allowedTools")
        ask = entry.get("askTools", [])
        if (unknown or not isinstance(command, str) or not command
                or not isinstance(args, list) or not all(isinstance(item, str) for item in args)
                or (env is not None and (not isinstance(env, dict)
                                         or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())))
                or (entry.get("cwd") is not None and not isinstance(entry.get("cwd"), str))
                or not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed)
                or not isinstance(ask, list) or not all(isinstance(item, str) for item in ask)):
            raise ValueError(f"MCP server {name!r}: expected command, optional args/env/cwd and an allowedTools list"
                             + (f"; unknown keys {sorted(unknown)}" if unknown else "") + ".")
        both = set(allowed) & set(ask)
        if both:
            raise ValueError(f"MCP server {name!r}: {sorted(both)} listed in both allowedTools and askTools; choose one rule.")
        parsed.append(_Server(name, command, list(args), dict(env) if env else None, entry.get("cwd"), list(allowed), list(ask)))
    return parsed


def _exclusion(server: _Server, tool: Any) -> str | None:
    if not TOOL_NAME.fullmatch(tool.name):
        return "unsupported_name"
    if tool.name not in server.allowed and tool.name not in server.ask:
        return "not_allowed"
    annotations = tool.annotations
    if tool.name in server.allowed and annotations is not None and (
            annotations.read_only_hint is False or annotations.destructive_hint is True):
        return "declares_side_effects"  # list it in askTools to allow it with approval
    schema = tool.input_schema
    if not isinstance(schema, dict) or schema.get("type") != "object" or len(json.dumps(schema).encode()) > MAX_SCHEMA_BYTES:
        return "unsupported_schema"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return "unsupported_schema"
    return None


class McpHub:
    def __init__(self, servers: list[_Server], *, oauth_redirect_url: str | None = None,
                 token_directory: Path | None = None) -> None:
        if any(server.oauth for server in servers) and (oauth_redirect_url is None or token_directory is None):
            raise ValueError("An MCP server signs in with OAuth, but the workbench has no OAuth callback address.")
        self._servers = {server.name: server for server in servers}
        self._stack = AsyncExitStack()
        self._oauth_redirect_url = oauth_redirect_url
        self._token_directory = token_directory
        self._sign_ins: dict[str, tuple[str, asyncio.Future[Any]]] = {}  # OAuth state -> (server, waiting callback)
        self._sessions: list[asyncio.Task[None]] = []
        self._closing = asyncio.Event()

    @classmethod
    def from_config(cls, path: Path, *, oauth_redirect_url: str | None = None,
                    token_directory: Path | None = None) -> McpHub:
        return cls(load_config(path), oauth_redirect_url=oauth_redirect_url, token_directory=token_directory)

    async def start(self) -> None:
        """Connect to every configured server; a server that fails is reported, not fatal.

        OAuth servers connect in a task of their own, which may wait for the user to sign in.
        """
        import httpx2
        from mcp import Client, StdioServerParameters  # the SDK is only needed when MCP is configured
        from mcp.client.streamable_http import streamable_http_client

        for server in self._servers.values():
            if server.oauth:
                server.status = "connecting"
                self._sessions.append(asyncio.create_task(self._oauth_session(server)))
                continue
            try:
                async with asyncio.timeout(START_TIMEOUT_SECONDS):
                    if server.url is not None:
                        # This app owns the HTTP client so it can carry the headers; the SDK keeps redirects on-origin.
                        http = await self._stack.enter_async_context(httpx2.AsyncClient(
                            headers=read_headers(server.header_env, bearer_env=server.bearer_env), timeout=httpx2.Timeout(30.0, read=300.0)))
                        # Streamable HTTP as servers deploy it today: the SDK's "auto" negotiation failed against the
                        # SDK's own HTTP server in testing (a probe session was rejected), while "legacy" connects.
                        server.client = await self._stack.enter_async_context(
                            Client(streamable_http_client(server.url, http_client=http), mode="legacy"))
                    else:
                        assert server.command is not None
                        server.client = await self._stack.enter_async_context(Client(StdioServerParameters(
                            command=server.command, args=server.args, env=server.env, cwd=server.cwd)))
                    await self._register_tools(server)
            except Exception as error:  # a broken third-party server must not stop the workbench
                server.status, server.error = "unavailable", _cause(error)

    async def _register_tools(self, server: _Server) -> None:
        listed: list[Any] = []
        cursor: str | None = None
        while True:
            page = await server.client.list_tools(cursor=cursor)
            listed.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                break
        for tool in sorted(listed, key=lambda item: item.name):
            reason = _exclusion(server, tool)
            if reason is not None:
                server.excluded.append({"tool": tool.name, "reason": reason})
                continue
            name = model_name(server.name, tool.name)
            description = " ".join((tool.description or "").split())[:MAX_DESCRIPTION_CHARS]
            server.tools[name] = McpTool(name, server.name, tool.name, description, dict(tool.input_schema),
                                         approval=tool.name in server.ask)
        server.status = "connected"

    async def _oauth_session(self, server: _Server) -> None:
        """Own one OAuth server's connection for the app's lifetime: sign in if needed, then stay connected.

        The SDK runs discovery, dynamic client registration, PKCE and the authorization code exchange.
        When the user is needed, the authorization URL is published in the status and this task waits
        for the workbench callback with the matching `state`.
        """
        import httpx2
        from mcp import Client
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

        assert server.url is not None and self._token_directory is not None and self._oauth_redirect_url is not None
        waiting: asyncio.Future[Any] | None = None

        async def redirect(url: str) -> None:
            nonlocal waiting
            state = parse_qs(urlsplit(url).query).get("state", [""])[0]
            if not state:
                raise RuntimeError("the authorization URL carries no state")
            waiting = asyncio.get_running_loop().create_future()
            self._sign_ins[state] = (server.name, waiting)
            server.status, server.authorization_url = "needs_sign_in", url

        async def callback() -> Any:
            assert waiting is not None
            try:
                return await asyncio.wait_for(waiting, SIGN_IN_TIMEOUT_SECONDS)
            finally:
                server.authorization_url = None
                server.status = "connecting"

        provider = OAuthClientProvider(
            server_url=server.url,
            client_metadata=OAuthClientMetadata.model_validate({
                "client_name": "Agent Harness", "redirect_uris": [self._oauth_redirect_url],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none", **({"scope": server.oauth_scope} if server.oauth_scope else {})}),
            storage=_FileTokenStorage(self._token_directory / f"{server.name}.json"),
            redirect_handler=redirect, callback_handler=callback)
        try:
            async with httpx2.AsyncClient(auth=provider, headers=read_headers(server.header_env),
                                          timeout=httpx2.Timeout(30.0, read=300.0)) as http:
                async with Client(streamable_http_client(server.url, http_client=http), mode="legacy") as client:
                    server.client = client
                    await self._register_tools(server)
                    await self._closing.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # sign-in timed out, was refused, or the server failed
            server.status, server.error = "unavailable", _cause(error)
        finally:
            server.client = None

    def complete_sign_in(self, state: str, code: str, iss: str | None = None) -> str | None:
        """Hand an authorization code to the session waiting for this `state`; the server name, or None."""
        from mcp.shared.auth import AuthorizationCodeResult

        entry = self._sign_ins.pop(state, None)
        if entry is None or entry[1].done():
            return None
        name, waiting = entry
        waiting.set_result(AuthorizationCodeResult.model_validate(
            {"code": code, "state": state, **({"iss": iss} if iss else {})}))
        return name

    async def close(self) -> None:
        self._closing.set()
        for _, waiting in self._sign_ins.values():
            waiting.cancel()
        if self._sessions:
            done, pending = await asyncio.wait(self._sessions, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self._sessions, return_exceptions=True)
        await self._stack.aclose()

    def status(self) -> Record:
        return {"servers": {name: {
            "status": server.status, **({"error": server.error} if server.error else {}),
            **({"authorization_url": server.authorization_url} if server.authorization_url else {}),
            "transport": "http" if server.url is not None else "stdio", **({"url": server.url} if server.url else {}),
            **({"oauth": True} if server.oauth else {}),
            "tools": [{"name": tool.name, "description": tool.description,
                       "approval": "required" if tool.approval else "none"} for tool in server.tools.values()],
            "excluded": list(server.excluded),
        } for name, server in self._servers.items()}}

    def tool(self, name: str) -> McpTool | None:
        match = MODEL_NAME.fullmatch(name)
        server = self._servers.get(match[1]) if match else None
        return server.tools.get(name) if server is not None else None

    def select(self, names: list[str]) -> dict[str, McpTool]:
        """The tools a Run opted into; any that are not offered right now make the request unavailable."""
        selected: dict[str, McpTool] = {}
        for name in names:
            tool = self.tool(name)
            if tool is None:
                raise RunFailure("mcp_tool_unavailable", f"{name} is not an available read-only MCP tool.")
            selected[name] = tool
        return selected

    async def call(self, tool: McpTool, arguments: Record) -> Record:
        server = self._servers[tool.server]
        if server.status != "connected" or server.client is None:
            raise RunFailure("mcp_unavailable", f"MCP server {tool.server} is not connected.")
        try:
            result = await server.client.call_tool(tool.tool, arguments, read_timeout_seconds=CALL_TIMEOUT_SECONDS)
        except Exception as error:
            raise RunFailure("mcp_unavailable", f"MCP server {tool.server} did not answer: {type(error).__name__}.") from error
        parts = [block.text if getattr(block, "type", None) == "text" else f"[{getattr(block, 'type', 'unknown')} content omitted]"
                 for block in result.content]
        text = "\n".join(parts)
        if result.is_error:
            raise RunFailure("mcp_tool_error", f"{tool.server}/{tool.tool} returned an error: {text[:500]}",
                             correctable_fields=tuple(arguments))
        size = len(text.encode("utf-8"))
        if size > MAX_RESULT_BYTES:
            raise RunFailure("mcp_result_too_large",
                             f"{tool.server}/{tool.tool} returned {size} bytes; narrow the request (limit {MAX_RESULT_BYTES}).",
                             correctable_fields=tuple(arguments))
        return mcp_evidence(tool, arguments, text)


def mcp_evidence(tool: McpTool, arguments: Record, text: str) -> Record:
    return {"evidence_type": "mcp_result", "path": f"mcp/{tool.server}/{tool.tool}", "server": tool.server,
            "tool": tool.tool, "arguments": arguments, "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "bytes": len(text.encode("utf-8")),
            "line_start": 1, "line_end": max(1, len(text.splitlines()))}
