"""OAuth sign-in for remote MCP servers.

A remote server with `"oauth": true` connects through the MCP SDK's OAuth client (discovery,
dynamic client registration, PKCE, authorization code). When it needs the user, the server is
listed as `needs_sign_in` with an authorization URL; the authorization server sends the browser
back to the workbench's callback, protected by the single-use OAuth `state` (a cross-site redirect
carries no SameSite=Strict cookie). Tokens are kept only in the store's `oauth/` folder, never in
the config, the API or events, and the next start signs in from them.

The authorization server here is the SDK's own OAuth server with an in-memory provider that
approves every request, standing in for the user's consent.
"""
from collections.abc import Iterator
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from agent_harness.mcp_hub import load_config
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from pydantic import AnyHttpUrl
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from test_completion import CompletionPeer

CALLBACK = "http://testserver/api/mcp/oauth/callback"
SEARCH = "mcp__notes__search_notes"


class MemoryProvider:
    """An authorization server that approves every request (standing in for the user's consent)."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.tokens: dict[str, AccessToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_hex(16)
        assert client.client_id is not None
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        del self.codes[authorization_code.code]
        token = secrets.token_hex(16)
        assert client.client_id is not None
        self.tokens[token] = AccessToken(token=token, client_id=client.client_id, scopes=authorization_code.scopes,
                                         expires_at=int(time.time()) + 3600, resource=authorization_code.resource)
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=3600)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> None:
        return None

    async def exchange_refresh_token(self, *args: object) -> OAuthToken:
        raise NotImplementedError

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.tokens.get(token)

    async def revoke_token(self, token: object) -> None:
        return None


@pytest.fixture(scope="module")
def oauth_server() -> Iterator[str]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    # The endpoint is /notes, not /mcp: on the machine these tests were written on, local HTTP responses
    # to paths ending in /mcp were rewritten in transit (chunked, body dropped) by software outside Python.
    server = MCPServer("notes", auth_server_provider=MemoryProvider(), auth=AuthSettings(
        issuer_url=AnyHttpUrl(base), resource_server_url=AnyHttpUrl(f"{base}/notes"), validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True)))

    @server.tool()
    def search_notes(query: str) -> str:
        """Return the notes whose text contains the query."""
        return "# Support handoff\nOwner: Mei\n" if "handoff" in query.lower() else "No matching notes."

    app = server.streamable_http_app(streamable_http_path="/notes")
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=runner.run, daemon=True).start()
    while not runner.started:
        time.sleep(0.05)
    # Some local security software rewrites HTTP responses to requests that carry the MCP protocol header
    # (the body is dropped), even on loopback. Where that happens the flow cannot run, so say so plainly.
    try:
        probe = httpx.get(f"{base}/.well-known/oauth-protected-resource/notes",
                          headers={"MCP-Protocol-Version": "2025-11-25"}, timeout=10)
        intact = probe.json().get("resource") == f"{base}/notes"
    except (httpx.HTTPError, ValueError):
        intact = False
    if not intact:
        runner.should_exit = True
        pytest.skip("Local software on this machine rewrites MCP HTTP responses on loopback; the OAuth flow runs in CI.")
    yield f"{base}/notes"
    runner.should_exit = True


def _app(tmp_path: Path, url: str) -> TestClient:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"notes": {"url": url, "oauth": True, "allowedTools": ["search_notes"]}}}),
                      encoding="utf-8")
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer([])),
                                 mcp_config=config, oauth_redirect_url=CALLBACK))


def _server_status(client: TestClient, wanted: set[str]) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = client.get("/api/config").json()["mcp"]["servers"]["notes"]
        if status["status"] in wanted:
            return status
        time.sleep(0.05)
    pytest.fail(f"MCP server stayed {status}")


def test_sign_in_connects_and_the_next_start_reuses_the_tokens(tmp_path: Path, oauth_server: str) -> None:
    with _app(tmp_path, oauth_server) as client:
        pending = _server_status(client, {"needs_sign_in"})
        assert pending["authorization_url"].startswith(oauth_server.removesuffix("/notes"))
        # The user's browser: the authorization server approves and redirects to the workbench callback.
        redirect = httpx.get(pending["authorization_url"], follow_redirects=False, timeout=10)
        assert redirect.status_code in {302, 303, 307}
        location = urlsplit(redirect.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == CALLBACK
        landing = client.get(f"{location.path}?{location.query}", headers={"Cookie": ""})
        assert landing.status_code == 200 and "signed in" in landing.text.lower()
        connected = _server_status(client, {"connected", "unavailable"})
        config_text = json.dumps(client.get("/api/config").json())

    assert connected["status"] == "connected", connected
    assert [tool["name"] for tool in connected["tools"]] == [SEARCH]
    stored = json.loads((tmp_path / "store" / "oauth" / "notes.json").read_text(encoding="utf-8"))
    assert stored["tokens"]["access_token"] and stored["tokens"]["access_token"] not in config_text
    with _app(tmp_path, oauth_server) as again:
        assert _server_status(again, {"connected", "needs_sign_in", "unavailable"})["status"] == "connected"


def test_a_callback_with_an_unknown_state_is_refused(tmp_path: Path, oauth_server: str) -> None:
    with _app(tmp_path, oauth_server) as client:
        _server_status(client, {"needs_sign_in"})
        response = client.get("/api/mcp/oauth/callback?code=abc&state=not-a-real-state")
        missing = client.get("/api/mcp/oauth/callback?state=x")
    assert response.status_code == 400 and missing.status_code == 400


def test_the_callback_needs_no_workbench_token_but_other_routes_still_do(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    # The server is never reached here; only the workbench's own routes are exercised.
    config.write_text(json.dumps({"mcpServers": {"notes": {"url": "http://127.0.0.1:9/notes", "oauth": True,
                                                            "allowedTools": ["search_notes"]}}}),
                      encoding="utf-8")
    app = create_app(tmp_path / "store", mcp_config=config, oauth_redirect_url=CALLBACK, access_token="callback-test-token-0123456789")
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/mcp/oauth/callback?code=abc&state=unknown").status_code == 400  # reached the route


@pytest.mark.parametrize(("entry", "message"), [
    ({"url": "https://mcp.example.com/mcp", "oauth": True, "bearerTokenEnv": "TOKEN"}, "oauth"),
    ({"url": "https://mcp.example.com/mcp", "oauth": "yes"}, "oauth"),
    ({"url": "https://mcp.example.com/mcp", "oauth": {"scope": 3}}, "scope"),
    ({"command": sys.executable, "oauth": True}, "unknown"),
])
def test_oauth_configs_are_validated(tmp_path: Path, entry: dict, message: str) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"notes": {"allowedTools": [], **entry}}}), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_an_oauth_server_needs_a_callback_address(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"notes": {"url": "https://mcp.example.com/mcp", "oauth": True,
                                                            "allowedTools": []}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="callback"):
        create_app(tmp_path / "store", mcp_config=config)
