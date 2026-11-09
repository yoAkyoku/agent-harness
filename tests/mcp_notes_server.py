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


if __name__ == "__main__":
    server.run()
