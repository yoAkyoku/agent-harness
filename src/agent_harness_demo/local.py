"""Same-origin access for a local filesystem workbench."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


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
