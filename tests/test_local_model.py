import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_workbench import ReportPeer, create_task, finish, tool_reply


class CitedReportPeer(ReportPeer):
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        response = await super().__call__(request)
        message = response.json()["choices"][0]["message"]
        calls = message.get("tool_calls", [])
        if calls and calls[0]["function"]["name"] == "artifact_write":
            function = calls[0]["function"]
            arguments = json.loads(function["arguments"])
            arguments["citations"] = ["E1"]
            function["arguments"] = json.dumps(arguments)
            return httpx.Response(200, json={"choices": [{"message": message}]})
        return response


class LocalPeer:
    def __init__(self) -> None:
        self.report = CitedReportPeer()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "contract-server"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "contract-model", "digest": "a" * 64}]})
        if request.url.path == "/api/show":
            assert json.loads(request.content) == {"model": "contract-model"}
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        assert str(request.url) == "http://127.0.0.1:11434/v1/chat/completions"
        return await self.report(request)


def qwen_thinking_metadata() -> dict:
    from test_session_versions import qwen_metadata
    metadata = qwen_metadata()
    metadata["capabilities"].append("thinking")
    metadata["thinking"] = {"values": [False, True], "default": True}
    return metadata


def test_configured_local_model_uses_same_loop_and_saves_provider_identity(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = LocalPeer()
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        config = client.get("/api/config").json()
        assert config["mode"] == "local"
        assert config["model"]["name"] == "contract-model"
        run, _ = finish(client, create_task(client, source))
        assert run["status"] == "completed", run
        assert run["mode"] == "local"
        assert run["model"]["name"] == "contract-model"
        assert run["model"]["digest"] == "a" * 64
        assert run["model"]["server_version"] == "contract-server"
        assert run["model"]["transport_fixture"] is True
        assert all(request["model"] == "contract-model" for request in peer.report.requests)
        text = client.get(run["artifacts"][0]["download_url"]).text
        assert text.startswith("# Snapshot\nRevenue was 42.\n")
        assert "facts.md:L1-L1" in text and run["sources"][0]["sha256"] in text


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def sse(value: dict | str) -> bytes:
    return ("data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n").encode()


class StreamingPeer(LocalPeer):
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return await super().__call__(request)
        body = json.loads(request.content)
        assert body["stream"] is True
        message = (await self.report(request)).json()["choices"][0]["message"]
        frames = [sse({"choices": [{"delta": {"reasoning": "hidden-provider-field",
                       "reasoning_content": "hidden-provider-field"}, "finish_reason": None}]})]
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            args = call["function"]["arguments"]
            middle = max(1, len(args) // 2)
            frames.append(sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": call["id"], "type": "function",
                "function": {"name": call["function"]["name"], "arguments": args[:middle]},
            }]}, "finish_reason": None}]}))
            frames.append(sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": args[middle:]},
            }]}, "finish_reason": None}]}))
            reason = "tool_calls"
        else:
            for text in ["Saved ", "the source snapshot."]:
                frames.append(sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]}))
            reason = "stop"
        frames.append(sse({"choices": [{"delta": {}, "finish_reason": reason}]}))
        frames.append(sse({"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54}}))
        frames.append(sse("[DONE]"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(frames))


def test_local_stream_reassembles_tools_and_emits_only_visible_text(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = StreamingPeer()
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model"),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "completed", run["error"]
        assert run["usage"] == {"model_calls": 4, "tool_calls": 3}
        assert run["result"] == "Saved the source snapshot."
        assert "".join(event["data"]["text"] for event in events if event["kind"] == "model.text_delta") == run["result"]
        assert "hidden-provider-field" not in json.dumps(events)
        replies = [event for event in events if event["kind"] == "model.responded"]
        assert replies[-1]["data"]["usage"] == {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54}
        text = client.get(run["artifacts"][0]["download_url"]).text
        assert text.startswith("# Snapshot\nRevenue was 42.\n")
        assert "facts.md:L1-L1" in text and run["sources"][0]["sha256"] in text


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_thinking_profile_is_recorded_without_exposing_reasoning(tmp_path: Path, stream: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    scripted = StreamingPeer() if stream else LocalPeer()
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        response = await scripted(request)
        if not stream and request.url.path == "/v1/chat/completions":
            payload = response.json()
            payload["choices"][0]["message"].update({"reasoning": "hidden-provider-field", "reasoning_content": "hidden-provider-field"})
            return httpx.Response(200, json=payload)
        return response
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=stream, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        config = client.get("/api/config").json()
        assert config["model"]["reasoning_effort"] == "low"
        assert config["model"]["context_profile"] == "ollama-0.40.10-qwen35-gpt2-thinking"
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "completed", run["error"]
        assert run["model"]["reasoning_effort"] == "low"
        assert all(body["reasoning_effort"] == "low" and body["temperature"] == 0
                   for body in scripted.report.requests)
        assert all(body["max_tokens"] == 1024 for body in scripted.report.requests)
        budgets = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"]
        assert all(budget["method"] == "qwen35_rendered_byte_bpe_upper_bound" for budget in budgets)
        assert all(budget["upper_bound_tokens"] <= 8192 for budget in budgets)
        assert "hidden-provider-field" not in json.dumps(events)
        assert "hidden-provider-field" not in json.dumps(scripted.report.requests)
        assert "hidden-provider-field" not in client.get(run["artifacts"][0]["download_url"]).text


@pytest.mark.parametrize("values", [None, [False], ["low", "high"], [0, 1]])
def test_thinking_opt_in_rejects_unverified_controls_before_generation(tmp_path: Path, values: object) -> None:
    from agent_harness_demo.contracts import RunFailure
    scripted = LocalPeer()
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            metadata = qwen_thinking_metadata()
            if values is None:
                del metadata["thinking"]
            else:
                metadata["thinking"] = {"values": values, "default": True}
            return httpx.Response(200, json=metadata)
        return await scripted(request)
    with pytest.raises(RunFailure) as error:
        with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", thinking=True),
                                   model_transport=httpx.MockTransport(peer))):
            pass
    assert error.value.code == "model_thinking_unavailable"
    assert scripted.report.requests == []


@pytest.mark.parametrize("thinking", [False, True])
def test_nonstream_truncation_never_dispatches_parseable_tool_call(tmp_path: Path, thinking: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    metadata_peer = LocalPeer()
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        if request.url.path != "/v1/chat/completions":
            return await metadata_peer(request)
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            response = tool_reply("source_read", {"path": "facts.md"}, "truncated-read").json()
            response["choices"][0]["finish_reason"] = "length"
            return httpx.Response(200, json=response)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}, "finish_reason": "stop"}]})
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=thinking),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "model_output_truncated"
        assert len(requests) == 1
        assert not any(event["kind"] == "tool.dispatched" for event in events)
        assert not run["artifacts"]


def test_nonstream_truncated_final_cannot_complete_even_after_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    scripted = LocalPeer()
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        response = await scripted(request)
        if request.url.path == "/v1/chat/completions":
            payload = response.json()
            if not payload["choices"][0]["message"].get("tool_calls"):
                payload["choices"][0]["finish_reason"] = "length"
                return httpx.Response(200, json=payload)
        return response
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "model_output_truncated"
        assert len(run["artifacts"]) == 1
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith("# Snapshot\nRevenue was 42.\n")
        assert events[-1]["kind"] == "run.failed"
        assert not any(event["kind"] == "run.completed" for event in events)


@pytest.mark.parametrize("stream", [False, True])
def test_thinking_only_stop_has_safe_diagnostics_without_retaining_reasoning(tmp_path: Path, stream: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    metadata_peer = LocalPeer()
    usage = {"prompt_tokens": 42, "completion_tokens": 128, "total_tokens": 170}
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        if request.url.path != "/v1/chat/completions":
            return await metadata_peer(request)
        private = {"reasoning": "PRIVATE-TRACE-MARKER", "reasoning_content": "PRIVATE-TRACE-MARKER"}
        if not stream:
            return httpx.Response(200, json={"choices": [{"message": {"content": None, **private},
                                                        "finish_reason": "stop"}], "usage": usage})
        frames = sse({"choices": [{"delta": private, "finish_reason": None}]})
        frames += sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        frames += sse({"choices": [], "usage": usage}) + sse("[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream([frames]))
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=stream, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed" and run["error"]["code"] == "model_protocol_error"
        expected = {"reason": "empty_response", "finish_reason": "stop",
            "visible_text_characters": 0, "tool_calls_count": 0, "reasoning_observed": True, "usage": usage}
        if stream:
            expected["stream_shape"] = {"data_events": 4, "content_fragments": 0, "tool_fragments": 0}
        assert run["error"]["diagnostics"] == expected
        assert run["usage"] == {"model_calls": 1, "tool_calls": 0}
        assert not run["artifacts"]
        assert "PRIVATE-TRACE-MARKER" not in json.dumps({"run": run, "events": events})
        assert not any(event["kind"] in {"tool.dispatched", "run.completed"} for event in events)


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_tool_diagnostics_do_not_echo_provider_arguments(tmp_path: Path, stream: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    metadata_peer = LocalPeer()
    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return await metadata_peer(request)
        if stream:
            frames = sse({"choices": [{"delta": {"content": "Visible.", "tool_calls": [{"index": 0,
                "id": "bad", "function": {"name": "source_read", "arguments": "PRIVATE-"}}]}}]})
            frames += sse({"choices": [{"delta": {"tool_calls": [{"index": 0,
                "function": {"arguments": "ARGUMENT-MARKER"}}]}}]})
            frames += sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}) + sse("[DONE]")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream([frames[:33], frames[33:]]))
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "bad", "type": "function", "function": {
                "name": "source_read", "arguments": "PRIVATE-ARGUMENT-MARKER"}}]}}]})
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=stream),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed"
        expected = {"reason": "invalid_tool_or_message", "finish_reason": "tool_calls",
            "visible_text_characters": 8 if stream else 0, "tool_calls_count": 1, "reasoning_observed": False, "usage": None}
        if stream:
            expected["stream_shape"] = {"data_events": 4, "content_fragments": 1, "tool_fragments": 2}
        assert run["error"]["diagnostics"] == expected
        assert "PRIVATE-ARGUMENT-MARKER" not in json.dumps({"run": run, "events": events})
        assert not any(event["kind"] == "tool.dispatched" for event in events)


@pytest.mark.parametrize("ending", [b"", sse({"choices": [{"delta": {}, "finish_reason": "length"}]}) + sse("[DONE]")])
def test_incomplete_stream_never_dispatches_partial_tool_call(tmp_path: Path, ending: bytes) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("A fact", encoding="utf-8")
    metadata = LocalPeer()

    async def truncated(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return await metadata(request)
        fragment = sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "partial", "function": {
            "name": "artifact_write", "arguments": '{"name":"bad.md","content":"partial',
        }}]}, "finish_reason": None}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream([fragment, ending]))

    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model"),
                               model_transport=httpx.MockTransport(truncated))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed"
        assert run["error"]["code"] in {"model_protocol_error", "model_output_truncated"}
        assert not run["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" for event in events)


def test_local_model_cannot_publish_unsupported_content_without_citations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Revenue was 42.", encoding="utf-8")
    metadata = LocalPeer()

    async def unsupported(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return await metadata(request)
        messages = json.loads(request.content)["messages"]
        if not any(item["role"] == "tool" for item in messages):
            return tool_reply("artifact_write", {"name": "unsupported.md", "content": "Revenue was 999."}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Finished."}}]})

    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False),
                               model_transport=httpx.MockTransport(unsupported))) as client:
        run, events = finish(client, create_task(client, source, limits={"max_corrections": 0}))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "missing_evidence"
        assert not run["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" for event in events)
