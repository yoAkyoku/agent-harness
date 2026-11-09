"""Local-only access for a filesystem workbench: same origin, a per-launch token, no framing."""

import hashlib
import secrets
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOKEN_ENV = "AGENT_HARNESS_TOKEN"
MIN_TOKEN_LENGTH = 16
SECURITY_HEADERS = {
    # The workbench is never meant to be embedded; refuse framing (clickjacking on approvals).
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    # The first page load carries the token in its query string; never leak it as a referrer.
    "referrer-policy": "no-referrer",
}


class LocalOriginMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            scheme = {"ws": "http", "wss": "https"}.get(scope["scheme"], scope["scheme"])
            expected = f"{scheme}://{headers.get('host', '')}"
            if origin is not None and origin.lower() != expected.lower():
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await JSONResponse({"detail": "Open this workbench from its own local address."}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def token_cookie_name(token: str) -> str:
    """A cookie name per token, so workbenches on other ports of the same host never overwrite it."""
    return "agent_harness_" + hashlib.sha256(token.encode()).hexdigest()[:16]


class AccessTokenMiddleware:
    """Require the per-launch access token on every request and WebSocket.

    A browser presents it once as `/?token=...`; that is exchanged for an HttpOnly,
    SameSite=Strict cookie and the token is removed from the address bar. Scripts send
    `Authorization: Bearer <token>`. Other local processes and users, and anything that
    reaches a published container port, cannot use the API without it.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"The access token must have at least {MIN_TOKEN_LENGTH} characters.")
        self.app = app
        self.token = token.encode()
        self.cookie = token_cookie_name(token)

    def _matches(self, value: str) -> bool:
        return secrets.compare_digest(value.encode(), self.token)

    def _presented(self, headers: Headers) -> bool:
        scheme, _, credential = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and self._matches(credential.strip()):
            return True
        return self._matches(cookie_parser(headers.get("cookie", "")).get(self.cookie, ""))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] == "/":
            offered = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token", [])
            if offered and self._matches(offered[-1]):
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(self.cookie, self.token.decode(), httponly=True, samesite="strict", path="/")
                await response(scope, receive, send)
                return
        if self._presented(Headers(scope=scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        await JSONResponse({"detail": "Open the address printed when the workbench started; it includes an access token."},
                           status_code=401)(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, with_headers)
