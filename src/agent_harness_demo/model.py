"""Chat Completions serialization and a clearly identified scripted transport."""

import json
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from .contracts import Decision, RunFailure, ToolCall
from .json_tools import decode_calls, project_request, strict_json_loads
from .context import ADDED_MESSAGE_PREFIX, request_budget
from .exact_context import ExactCounter, LIBRARY_SHA, MODEL_SHA, MODEL_DIGEST, PROFILE
from .streaming import MAX_RESPONSE_BYTES, TextSink, read_chat_stream, token_usage


class ModelResponseError(RunFailure):
    """Protocol failure with bounded shape metadata, never provider text."""

    def __init__(self, reason: Literal["empty_response", "invalid_tool_or_message", "output_truncated"], payload: Any,
                 *, stream_shape: dict[str, int] | None = None) -> None:
        if reason == "output_truncated":
            super().__init__("model_output_truncated", "The model did not complete its response within the output limit.")
        else:
            super().__init__("model_protocol_error", "The model response has an invalid tool or message format.")
        value = payload if isinstance(payload, dict) else {}
        choices = value.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        raw = choice.get("message")
        raw = raw if isinstance(raw, dict) else {}
        content, calls = raw.get("content"), raw.get("tool_calls")
        finish = choice.get("finish_reason")
        if finish is not None and (not isinstance(finish, str) or finish not in {"stop", "tool_calls", "length", "content_filter"}):
            finish = "other"
        self.diagnostics = {"reason": reason, "finish_reason": finish,
            "visible_text_characters": len(content) if isinstance(content, str) else 0 if content is None else None,
            "tool_calls_count": min(len(calls), 51) if isinstance(calls, list) else 0 if calls is None else None,
            "reasoning_observed": value.get("reasoning_observed") is True or any(bool(raw.get(field)) for field in ("reasoning", "reasoning_content", "thinking")),
            "usage": token_usage(value.get("usage"))}
        if stream_shape is not None:
            self.diagnostics["stream_shape"] = stream_shape


REMOTE_MODES = frozenset({"openai", "anthropic"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REMOTE_CONTEXT_PROFILE = "remote_utf8_byte_upper_bound"


def _validated_base_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError("Use an https base URL without credentials, query or fragment.")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("Plain HTTP is only allowed for a loopback model server.")
    return value.rstrip("/")


@dataclass(frozen=True)
class ModelSettings:
    mode: Literal["fixture", "local", "openai", "anthropic"] = "fixture"
    name: str = "fixture-script-v1"
    stream: bool = False
    context_window_tokens: int = 8192
    thinking: bool = False
    decision_protocol: Literal["native", "json_tools"] = "native"
    exact_tokenizer_library: Path | None = None
    exact_tokenizer_model: Path | None = None
    server_controlled_initial_plan: bool = False
    base_url: str | None = None
    api_key_env: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"

    def __post_init__(self) -> None:
        if self.mode in REMOTE_MODES:
            self._validate_remote()
            return
        if (self.exact_tokenizer_library is None) != (self.exact_tokenizer_model is None):
            raise ValueError("Specify both local tokenizer assets.")
        if self.exact_tokenizer_library is not None and (self.mode != "local" or self.thinking or self.decision_protocol != "json_tools"):
            raise ValueError("Exact local accounting requires nonthinking JSON tools.")
        if self.decision_protocol not in {"native", "json_tools"}:
            raise ValueError("Choose native or json_tools decision protocol.")
        if self.mode != "local" and self.decision_protocol != "native":
            raise ValueError("Structured decisions require explicit local model settings.")
        if type(self.thinking) is not bool:
            raise ValueError("Choose an explicit thinking boolean.")
        if type(self.server_controlled_initial_plan) is not bool:
            raise ValueError("Choose an explicit initial-plan policy.")
        if type(self.context_window_tokens) is not int or not 4096 <= self.context_window_tokens <= 262144:
            raise ValueError("Choose an explicit bounded context capacity.")
        if self.mode not in {"fixture", "local"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", self.name):
            raise ValueError("Choose a valid explicit local model name.")
        if self.mode == "local" and "cloud" in self.name.lower():
            raise ValueError("Cloud model identifiers are not supported.")
        if self.mode == "fixture" and (self.name != "fixture-script-v1" or self.stream or self.thinking):
            raise ValueError("The fixture profile uses its fixed non-streaming transport.")

    def _validate_remote(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", self.name):
            raise ValueError("Choose a valid explicit model name.")
        if self.stream or self.thinking or self.decision_protocol != "native" or self.exact_tokenizer_library or self.exact_tokenizer_model:
            raise ValueError("Remote providers use native, non-streaming tool calling without local tokenizer assets.")
        if type(self.context_window_tokens) is not int or not 4096 <= self.context_window_tokens <= 1_000_000:
            raise ValueError("Choose an explicit bounded context capacity.")
        if type(self.server_controlled_initial_plan) is not bool:
            raise ValueError("Choose an explicit initial-plan policy.")
        if not self.api_key_env or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", self.api_key_env):
            raise ValueError("Name the environment variable that holds the API key.")
        if self.mode == "openai":
            if self.base_url is None:
                raise ValueError("An OpenAI-compatible provider needs a base URL.")
            object.__setattr__(self, "base_url", _validated_base_url(self.base_url))
        elif self.base_url is not None:
            raise ValueError("The Claude adapter uses the official SDK endpoint.")
        if self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Choose a supported effort level.")

    @classmethod
    def openai_compatible(cls, name: str, *, base_url: str, api_key_env: str = "OPENAI_API_KEY",
                          context_window_tokens: int = 128_000,
                          server_controlled_initial_plan: bool = False) -> "ModelSettings":
        """Any server that speaks OpenAI Chat Completions with native tool calling."""
        return cls(mode="openai", name=name, base_url=base_url, api_key_env=api_key_env,
                   context_window_tokens=context_window_tokens,
                   server_controlled_initial_plan=server_controlled_initial_plan)

    @classmethod
    def anthropic(cls, name: str = "claude-opus-5-5", *, api_key_env: str = "ANTHROPIC_API_KEY",
                  context_window_tokens: int = 200_000, effort: Literal["low", "medium", "high", "xhigh", "max"] = "low",
                  server_controlled_initial_plan: bool = False) -> "ModelSettings":
        """Claude through the official Anthropic SDK (Messages API)."""
        return cls(mode="anthropic", name=name, api_key_env=api_key_env, context_window_tokens=context_window_tokens,
                   effort=effort, server_controlled_initial_plan=server_controlled_initial_plan)

    @property
    def loopback(self) -> bool:
        return self.base_url is not None and urlsplit(self.base_url).hostname in LOOPBACK_HOSTS

    @classmethod
    def local(cls, name: str, *, stream: bool = True, thinking: bool = False,
              decision_protocol: Literal["native", "json_tools"] = "native",
              server_controlled_initial_plan: bool = False,
              exact_tokenizer_library: Path | None = None, exact_tokenizer_model: Path | None = None) -> "ModelSettings":
        return cls(mode="local", name=name, stream=stream, thinking=thinking, decision_protocol=decision_protocol,
                   server_controlled_initial_plan=server_controlled_initial_plan,
                   exact_tokenizer_library=exact_tokenizer_library, exact_tokenizer_model=exact_tokenizer_model)

    @property
    def endpoint(self) -> str:
        if self.mode == "local":
            return "http://127.0.0.1:11434/v1/chat/completions"
        if self.mode == "openai":
            return f"{self.base_url}/chat/completions"
        if self.mode == "anthropic":
            return "https://api.anthropic.com/v1/messages"
        return "https://fixture.invalid/v1/chat/completions"


class DecisionModel(Protocol):
    """What the runtime needs from a model provider; the harness loop owns everything else."""

    settings: ModelSettings
    info: dict[str, Any]

    async def context_budget(self, body: dict[str, Any], capacity: int, reserve: int, profile: str) -> dict[str, Any]: ...

    def request_body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_output_tokens: int) -> dict[str, Any]: ...

    async def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_text: TextSink | None = None,
                     *, max_output_tokens: int = 2048) -> Decision: ...


class ModelAdapter:
    def __init__(self, client: httpx.AsyncClient, settings: ModelSettings, *, transport_fixture: bool) -> None:
        self.client = client
        self.settings = settings
        self.exact_counter: ExactCounter | None = None
        self._headers: dict[str, str] = {}
        configured = settings.mode != "fixture"
        self.info: dict[str, Any] = {
            "mode": settings.mode, "name": settings.name, "endpoint": settings.endpoint,
            "stream": settings.stream, "transport_fixture": transport_fixture, "decision_protocol": settings.decision_protocol,
            "requires_tool_finish": settings.decision_protocol == "json_tools",
            # Remote models get task_finish after publication, so their completion is checked too.
            "offers_tool_finish": settings.mode == "openai",
            "context_capacity_tokens": settings.context_window_tokens if configured else 16777216,
            "context_capacity_source": "application_configuration" if configured else "synthetic_fixture",
            "context_profile": REMOTE_CONTEXT_PROFILE if settings.mode == "openai" else "synthetic_fixture",
        }
        if settings.server_controlled_initial_plan:
            self.info["server_controlled_initial_plan"] = True

    async def prepare(self) -> None:
        if self.settings.mode == "fixture":
            return
        if self.settings.mode == "openai":
            assert self.settings.api_key_env is not None
            key = os.environ.get(self.settings.api_key_env, "")
            if key:
                self._headers = {"Authorization": f"Bearer {key}"}
            elif not self.settings.loopback:
                raise RunFailure("model_credentials_missing",
                                 f"Set {self.settings.api_key_env} to the API key for {self.settings.base_url}.")
            return
        base = "http://127.0.0.1:11434"
        try:
            version = await self.client.get(base + "/api/version", timeout=5)
            tags = await self.client.get(base + "/api/tags", timeout=5)
            show = await self.client.post(base + "/api/show", json={"model": self.settings.name}, timeout=10)
            for response in (version, tags, show):
                response.raise_for_status()
            models = tags.json()["models"]
            found = next((model for model in models if model["name"] == self.settings.name), None)
            metadata = show.json()
            if metadata.get("remote_model") or metadata.get("remote_host"):
                raise RunFailure("remote_model_forbidden", "Only already-downloaded local model weights are supported.")
            if found is None or not re.fullmatch(r"[a-fA-F0-9]{64}", found.get("digest", "")):
                raise RunFailure("model_not_installed", "The configured local model is not present with a valid digest.")
            if "tools" not in metadata.get("capabilities", []):
                raise RunFailure("model_tools_unavailable", "The configured model does not advertise tool calling.")
            server_version = version.json()["version"]
            info = metadata.get("model_info", {})
            profile_matches = (server_version == "0.40.10" and metadata.get("renderer") == "qwen3.5"
                and metadata.get("parser") == "qwen3.5" and metadata.get("template_source") == "renderer_parser"
                and metadata.get("template") == "{{ .Prompt }}" and not metadata.get("messages") and not metadata.get("system")
                and info.get("general.architecture") == "qwen35" and info.get("tokenizer.ggml.model") == "gpt2"
                and info.get("tokenizer.ggml.pre") == "qwen35" and info.get("tokenizer.ggml.add_bos_token") is False)
            if profile_matches:
                self.info["context_profile"] = "ollama-0.40.10-qwen35-gpt2"
                self.info["renderer_verification"] = {"renderer": metadata["renderer"], "parser": metadata["parser"],
                    "template_source": metadata["template_source"], "implicit_messages": False, "implicit_system": False,
                    "tokenizer_model": info["tokenizer.ggml.model"], "tokenizer_pre": info["tokenizer.ggml.pre"], "add_bos_token": False}
            elif self.settings.thinking or not self.info["transport_fixture"] or metadata.get("renderer") == "qwen3.5" or server_version == "0.40.10":
                raise RunFailure("context_profile_unsupported", "The local renderer/tokenizer profile has no verified context bound.")
            if self.settings.thinking:
                controls = metadata.get("thinking")
                values = controls.get("values") if isinstance(controls, dict) else None
                if ("thinking" not in metadata["capabilities"] or not isinstance(values, list)
                        or not all(type(value) is bool for value in values) or not any(value is True for value in values)):
                    raise RunFailure("model_thinking_unavailable", "This verified profile requires explicit boolean thinking support.")
                self.info["context_profile"] = "ollama-0.40.10-qwen35-gpt2-thinking"
            self.info.update({"digest": found["digest"], "server_version": version.json()["version"],
                              "capabilities": metadata["capabilities"],
                              "reasoning_effort": "low" if self.settings.thinking else "none"})
            if self.settings.exact_tokenizer_library is not None and self.settings.exact_tokenizer_model is not None:
                if not profile_matches or found["digest"] != MODEL_DIGEST:
                    raise RunFailure("exact_context_unavailable", "Exact accounting has no verified profile for this model.")
                self.exact_counter = await asyncio.to_thread(ExactCounter, self.settings.exact_tokenizer_library, self.settings.exact_tokenizer_model)
                self.info["context_profile"] = PROFILE
                self.info["tokenizer_verification"] = {"library_sha256":LIBRARY_SHA,"model_blob_sha256":MODEL_SHA,
                    "vocab_only":True,"add_special":True,"parse_special":True,"renderer":"qwen3.5"}
        except httpx.HTTPError as error:
            raise RunFailure("model_unavailable", "Start the configured local Ollama service and use an already-installed model.") from error
        except (KeyError, TypeError, ValueError) as error:
            raise RunFailure("model_metadata_invalid", "The local model metadata could not be verified.") from error

    async def context_budget(self, body: dict[str, Any], capacity: int, reserve: int, profile: str) -> dict[str, Any]:
        if self.exact_counter is not None:
            return await self.exact_counter.budget(body, capacity, reserve)
        return request_budget(body, capacity, reserve, profile)

    def request_body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_output_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.settings.name, "messages": messages, "tools": tools,
                                "stream": self.settings.stream, "max_tokens": max_output_tokens}
        if self.settings.decision_protocol == "json_tools":
            projected, schema = project_request(messages, tools)
            body.update(messages=projected, tools=[], response_format={"type": "json_schema", "json_schema": {"schema": schema}})
        if self.settings.mode == "local":
            body.update({"temperature": 0, "reasoning_effort": "low" if self.settings.thinking else "none"})
        if self.settings.stream:
            body["stream_options"] = {"include_usage": True}
        return body

    async def decide(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_text: TextSink | None = None,
        *, max_output_tokens: int = 2048,
    ) -> Decision:
        stream_shape = None
        try:
            body = self.request_body(messages, tools, max_output_tokens)
            if self.settings.stream:
                async with self.client.stream("POST", self.settings.endpoint, json=body, headers=self._headers) as response:
                    response.raise_for_status()
                    payload = await read_chat_stream(response, on_text if self.settings.decision_protocol == "native" else None)
                    stream_shape = payload.pop("stream_shape")
            else:
                response = await self.client.post(self.settings.endpoint, json=body, headers=self._headers)
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise RunFailure("model_response_too_large", "The model response exceeded its size limit.")
                payload = response.json()
        except httpx.HTTPError as error:
            raise RunFailure("model_unavailable", "The model transport did not return a usable response.") from error
        except ValueError as error:
            raise RunFailure("model_protocol_error", "The model did not return valid JSON.") from error
        try:
            choice = payload["choices"][0]
            if choice.get("finish_reason") not in {None, "stop", "tool_calls"}:
                raise ModelResponseError("output_truncated", payload, stream_shape=stream_shape)
            raw = choice["message"]
            text = raw.get("content") or ""
            if not isinstance(text, str) or len(text.encode("utf-8")) > 262_144:
                raise ValueError
            if self.settings.decision_protocol == "json_tools":
                if raw.get("tool_calls") not in (None, []):
                    raise ValueError
                structured = decode_calls(text, tools)
                structured_message = {"role": "assistant", "content": None, "decision_protocol": "json_tools", "wire_content": text,
                    "tool_calls": [{"id": call.identity, "type": "function", "function": {"name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False)}} for call in structured]}
                return Decision("", structured, structured_message, token_usage(payload.get("usage")))
            calls = []
            tool_calls = raw.get("tool_calls") or []
            if not isinstance(tool_calls, list) or len(tool_calls) > 50:
                raise ValueError
            for item in tool_calls:
                arguments = strict_json_loads(item["function"]["arguments"])
                identity, name = item["id"], item["function"]["name"]
                if not isinstance(arguments, dict) or not isinstance(identity, str) or not identity:
                    raise ValueError
                if not isinstance(name, str):
                    raise ValueError
                calls.append(ToolCall(identity, name, arguments))
            if not calls and not text:
                raise ModelResponseError("empty_response", payload, stream_shape=stream_shape)
            message: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = raw["tool_calls"]
            return Decision(text, tuple(calls), message, token_usage(payload.get("usage")))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise ModelResponseError("invalid_tool_or_message", payload, stream_shape=stream_shape) from error


class FixtureTransport:
    """A deterministic source snapshot recipe; it does not interpret task intent."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        # The current task is the last user turn that is not a message added during the Run.
        current = max(index for index, message in enumerate(messages) if message["role"] == "user"
                      and not str(message.get("content") or "").startswith(ADDED_MESSAGE_PREFIX))
        results = [json.loads(item["content"]) for item in messages[current+1:] if item["role"] == "tool"]
        if not results:
            # Only plan_update is offered until a plan is saved.
            return self._call("plan_update", {"steps": ["列出來源", "讀取來源", "保存快照"]}, "plan")
        if len(results) == 1:
            return self._call("workspace_list", {}, "catalog")
        sources = results[1]["sources"]
        read_count = len(results) - 2
        if read_count < len(sources):
            return self._call("source_read", {"path": sources[read_count]["path"]}, f"read-{read_count}")
        if read_count == len(sources):
            sections = ["# 來源快照", "此成果由模擬模型依固定示範步驟建立。\n"]
            for result in results[2:]:
                sections.append(f"## {result['path']}\n\n{result['text']}")
            return self._call("artifact_write", {"name": "source-snapshot.md", "content": "\n\n".join(sections)}, "publish")
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "來源快照已保存，可在右側預覽或下載。",
        }}]})

    @staticmethod
    def _call(name: str, arguments: dict[str, Any], identity: str) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": identity, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
            }}],
        }}]})
