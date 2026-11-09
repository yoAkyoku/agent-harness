"""Claude through the official Anthropic SDK (Messages API).

The runtime keeps its conversation in Chat Completions shape. This adapter
translates each request to the Messages API and each response back, so the
harness loop, tools, verifiers and storage stay provider-independent.

Thinking blocks are never replayed. Current Claude models always think and
bind each thinking block to the exact conversation prefix that produced it,
while this harness deliberately rewrites history (context pruning, executor
state snapshots, publication finalization). Replaying a block after such an
edit is rejected, so the adapter keeps only text and tool calls; the model
then answers each turn without its previous turns' private reasoning.
"""

import copy
import json
import os
from typing import Any

import anthropic
import httpx2

from .context import request_budget
from .contracts import Decision, RunFailure, ToolCall
from .model import REMOTE_CONTEXT_PROFILE, ModelResponseError, ModelSettings, model_choices
from .streaming import TextSink

STATE_PREFIX = "Harness state (written by the agent runtime, not the user):\n"


def _tool_result(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content") or ""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content}
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        block["is_error"] = True
    return block


def _append(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """Merge consecutive same-role turns so roles alternate, as the API expects."""
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        previous = messages[-1]["content"]
        if isinstance(previous, str):
            previous = [{"type": "text", "text": previous}]
        messages[-1]["content"] = previous + blocks
    elif role == "user" and len(blocks) == 1 and blocks[0]["type"] == "text":
        messages.append({"role": role, "content": blocks[0]["text"]})
    else:
        messages.append({"role": role, "content": blocks})


def to_messages_api(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Chat Completions history -> (system prompt, Messages API turns)."""
    system = ""
    converted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "system" and index == 0:
            system = message.get("content") or ""
        elif role == "system":
            # Mid-conversation runtime state; the Messages API takes system text only at the top level.
            _append(converted, "user", [{"type": "text", "text": STATE_PREFIX + (message.get("content") or "")}])
        elif role == "user":
            _append(converted, "user", [{"type": "text", "text": message.get("content") or ""}])
        elif role == "tool":
            _append(converted, "user", [_tool_result(message)])
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                               "input": json.loads(call["function"]["arguments"])})
            _append(converted, "assistant", blocks)
    return system, converted


def to_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool["function"]
        item: dict[str, Any] = {"name": function["name"], "input_schema": function["parameters"]}
        if function.get("description"):
            item["description"] = function["description"]
        converted.append(item)
    return converted


class AnthropicAdapter:
    """Same interface as ModelAdapter: info, settings, prepare, request_body, context_budget, decide."""

    def __init__(self, settings: ModelSettings, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        if settings.mode != "anthropic":
            raise ValueError("AnthropicAdapter needs Claude settings.")
        self.settings = settings
        self.exact_counter = None
        self._transport = transport
        self.client: anthropic.AsyncAnthropic | None = None
        self.info: dict[str, Any] = {
            "mode": "anthropic", "name": settings.name, "endpoint": settings.endpoint, "stream": False,
            "transport_fixture": transport is not None, "decision_protocol": "native", "requires_tool_finish": False,
            "offers_tool_finish": True,
            "context_capacity_tokens": settings.context_window_tokens, "context_capacity_source": "application_configuration",
            "context_profile": REMOTE_CONTEXT_PROFILE, "effort": settings.effort, "thinking_replay": "never",
            "choices": model_choices(settings),
        }
        if settings.server_controlled_initial_plan:
            self.info["server_controlled_initial_plan"] = True

    def with_settings(self, settings: ModelSettings) -> "AnthropicAdapter":
        """A variant sharing this adapter's client, for the model and effort a Run chose."""
        if settings.mode != "anthropic":
            raise ValueError("The Claude adapter only switches Claude models and effort.")
        variant = copy.copy(self)
        variant.settings = settings
        variant.info = {**self.info, "name": settings.name, "effort": settings.effort}
        return variant

    async def prepare(self) -> None:
        assert self.settings.api_key_env is not None
        key = os.environ.get(self.settings.api_key_env, "")
        if not key:
            raise RunFailure("model_credentials_missing", f"Set {self.settings.api_key_env} to use {self.settings.name}.")
        http_client = anthropic.DefaultAsyncHttpxClient(transport=self._transport) if self._transport is not None else None
        self.client = anthropic.AsyncAnthropic(api_key=key, http_client=http_client, max_retries=2)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def context_budget(self, body: dict[str, Any], capacity: int, reserve: int, profile: str) -> dict[str, Any]:
        return request_budget(body, capacity, reserve, profile)

    def request_body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_output_tokens: int) -> dict[str, Any]:
        system, converted = to_messages_api(messages)
        body: dict[str, Any] = {"model": self.settings.name, "max_tokens": max_output_tokens, "system": system,
                                "messages": converted, "output_config": {"effort": self.settings.effort}}
        if tools:
            body["tools"] = to_tools(tools)
        return body

    async def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_text: TextSink | None = None,
                     *, max_output_tokens: int = 2048) -> Decision:
        if self.client is None:
            raise RunFailure("model_unavailable", "The Claude client was not prepared.")
        body = self.request_body(messages, tools, max_output_tokens)
        try:
            response = await self.client.messages.create(**body)
        except anthropic.APIConnectionError as error:
            raise RunFailure("model_unavailable", "The Claude API could not be reached.") from error
        except anthropic.RateLimitError as error:
            raise RunFailure("model_unavailable", "The Claude API rate limit was reached; try again later.") from error
        except anthropic.APIStatusError as error:
            if error.status_code >= 500:
                raise RunFailure("model_unavailable", "The Claude API returned a server error.") from error
            raise RunFailure("model_request_rejected", f"The Claude API rejected the request (HTTP {error.status_code}).") from error
        if response.stop_reason == "refusal":
            raise RunFailure("model_refused", "The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise ModelResponseError("output_truncated", {})
        if response.stop_reason not in {"end_turn", "tool_use", "stop_sequence"}:
            raise ModelResponseError("invalid_tool_or_message", {})
        text = "".join(block.text for block in response.content if block.type == "text")
        calls = []
        for block in response.content:
            if block.type == "tool_use":
                if not isinstance(block.input, dict) or not block.id:
                    raise ModelResponseError("invalid_tool_or_message", {})
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
        if not calls and not text:
            raise ModelResponseError("empty_response", {})
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = [{"id": call.identity, "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}} for call in calls]
        usage = {"prompt_tokens": response.usage.input_tokens, "completion_tokens": response.usage.output_tokens,
                 "total_tokens": response.usage.input_tokens + response.usage.output_tokens}
        return Decision(text, tuple(calls), message, usage)
