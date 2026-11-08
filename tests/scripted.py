"""Helpers for scripted model peers.

The harness offers only `plan_update` until a plan is saved, and it refuses calls to
tools a request did not offer. Many tests script a model that goes straight to the
behaviour under test; `planning` puts the plan step in front of such a script.
"""
import inspect
import json
from collections.abc import Awaitable, Callable
from itertools import count

import httpx

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
PLAN = ["Inspect the selected sources", "Do the task", "Publish the result"]
# Child services and recorder scripts started by tests read this from AGENT_HARNESS_TOKEN (see conftest).
SERVICE_TOKEN = "test-service-token-0123456789"


def offered_tools(request: httpx.Request) -> list[str]:
    return [tool["function"]["name"] for tool in json.loads(request.content).get("tools") or []]


def planning(handler: Handler) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """Answer the harness's planning-only request with a plan; the script handles the rest.

    The script never sees the planning request, so its first request is the first one
    that offers the full tool catalogue — the same request it saw before plans were
    enforced. Only native tool calls are supported; JSON-tools scripts must plan themselves.
    """
    identities = count(1)

    async def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions") and offered_tools(request) == ["plan_update"]:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"scripted-plan-{next(identities)}", "type": "function",
                    "function": {"name": "plan_update", "arguments": json.dumps({"steps": PLAN})},
                }],
            }}]})
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    return wrapped
