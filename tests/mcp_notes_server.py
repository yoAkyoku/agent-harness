"""A small stdio MCP server for tests, built with the official SDK."""
import os

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

NOTES = {
    "handoff": "# Support handoff\nOwner: Mei\nIncident escalation contact: Bo\n",
    "release": "# Release checklist\nOwner: Sora\nRelease gate: passing regression tests\n",
}
server = MCPServer("notes")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def search_notes(query: str) -> str:
    """Return the notes whose text contains the query."""
    found = [text for text in NOTES.values() if query.lower() in text.lower()]
    return "\n".join(found) if found else "No matching notes."


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
def delete_note(name: str) -> str:
    """Delete a note."""
    NOTES.pop(name, None)
    return f"Deleted {name}."


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def create_note(name: str, text: str) -> str:
    """Create or replace a note."""
    NOTES[name] = text
    return f"Saved note {name}."


@server.tool()
def word_count(text: str) -> int:
    """Count words in a text."""
    return len(text.split())


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def lookup_note(name: str) -> str:
    """Return one note by name; unknown names are an error result."""
    if name not in NOTES:
        raise ToolError(f"No note named {name}.")
    return NOTES[name]


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def env_has(name: str) -> str:
    """Report whether an environment variable reached this server."""
    return "present" if name in os.environ else "absent"


def _bearer_only(app, token: str):  # type: ignore[no-untyped-def]
    """Answer 401 to HTTP requests without `Authorization: Bearer <token>`; lifespan passes through."""
    async def guarded(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != f"Bearer {token}".encode():
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"missing or wrong token"})
            return
        await app(scope, receive, send)
    return guarded


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["--http"]:  # Streamable HTTP on the given port, behind NOTES_HTTP_TOKEN
        import uvicorn

        uvicorn.run(_bearer_only(server.streamable_http_app(), os.environ["NOTES_HTTP_TOKEN"]),
                    host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    else:
        server.run()
