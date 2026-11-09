"""Bounded SSE assembly. Tool calls are returned only after a complete stream."""

import codecs
from collections.abc import Awaitable, Callable
import json
from typing import Any

import httpx

from .contracts import RunFailure

TextSink = Callable[[str], Awaitable[None]]
MAX_RESPONSE_BYTES = 8_388_608


def token_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result = {key: number for key in ("prompt_tokens", "completion_tokens", "total_tokens")
              if isinstance(number := value.get(key), int) and not isinstance(number, bool) and number >= 0}
    return result or None


async def read_chat_stream(response: httpx.Response, on_text: TextSink | None) -> dict[str, Any]:
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        raise RunFailure("model_protocol_error", "The configured streaming model did not return SSE.")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    lines: list[str] = []
    text: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, int] | None = None
    finish: str | None = None
    done = False
    total_bytes = 0
    text_bytes = 0
    reasoning_observed = False
    stream_shape = {"data_events": 0, "content_fragments": 0, "tool_fragments": 0}

    async def consume(data: str) -> None:
        nonlocal usage, finish, done, text_bytes, reasoning_observed
        stream_shape["data_events"] += 1
        if data == "[DONE]":
            done = True
            return
        value = json.loads(data)
        received_usage = token_usage(value.get("usage"))
        if received_usage is not None:
            usage = received_usage
        choices = value.get("choices", [])
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        if finish is not None and delta:
            raise ValueError("Content after completion")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("Invalid content")
            text_bytes += len(content.encode("utf-8"))
            if text_bytes > 262_144:
                raise RunFailure("model_response_too_large", "The model text exceeded its size limit.")
            text.append(content)
            if content:
                stream_shape["content_fragments"] += 1
            if content and on_text is not None:
                await on_text(content)
        # Reasoning/thinking fields are neither displayed nor retained in the visible conversation.
        reasoning_observed = reasoning_observed or any(bool(delta.get(field)) for field in ("reasoning", "reasoning_content", "thinking"))
        for piece in delta.get("tool_calls") or []:
            stream_shape["tool_fragments"] += 1
            index = piece["index"]
            if type(index) is not int or not 0 <= index < 50:
                raise ValueError("Invalid tool index")
            call = calls.setdefault(index, {"id": [], "name": [], "arguments": []})
            function = piece.get("function") or {}
            for field, fragment in (("id", piece.get("id")), ("name", function.get("name")),
                                    ("arguments", function.get("arguments"))):
                if fragment is not None:
                    if not isinstance(fragment, str):
                        raise ValueError("Invalid tool fragment")
                    call[field].append(fragment)
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason not in {"stop", "tool_calls"}:
                raise RunFailure("model_output_truncated", "The model did not complete its response within the output limit.",
                    diagnostics={"reason": "output_truncated", "finish_reason": reason if reason in {"length", "content_filter"} else "other",
                        "visible_text_characters": sum(len(part) for part in text), "tool_calls_count": len(calls),
                        "reasoning_observed": reasoning_observed, "usage": usage, "stream_shape": dict(stream_shape)})
            finish = reason

    try:
        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > MAX_RESPONSE_BYTES:
                raise RunFailure("model_response_too_large", "The model stream exceeded its size limit.")
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.removesuffix("\r")
                if not line:
                    if lines:
                        await consume("\n".join(lines))
                        lines.clear()
                        if done:
                            break
                elif line.startswith("data:"):
                    lines.append(line[5:].removeprefix(" "))
            if done:
                break
        if not done or finish is None:
            raise ValueError("Incomplete SSE response")
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            if sorted(calls) != list(range(len(calls))):
                raise ValueError("Missing tool index")
            message["tool_calls"] = [{"id": "".join(call["id"]), "type": "function", "function": {
                "name": "".join(call["name"]), "arguments": "".join(call["arguments"]),
            }} for _, call in sorted(calls.items())]
        return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage,
                "reasoning_observed": reasoning_observed, "stream_shape": stream_shape}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        raise RunFailure("model_protocol_error", "The model stream was incomplete or malformed; no partial tool call was dispatched.") from error
