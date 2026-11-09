from pathlib import Path
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.web import create_app
from test_workbench import ReportPeer, create_task, finish, tool_reply


def test_revision_pins_base_and_keeps_original_download(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    initial = ReportPeer()
    revision_requests: list[dict] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not any(item.get("role") == "user" and item.get("content") == "Retitle the base as Revised." for item in body["messages"]):
            return await initial(request)
        revision_requests.append(body)
        if len(revision_requests) == 1:
            return tool_reply("plan_update", {"steps": ["Revise the pinned base"]}, "revise-plan")
        if len(revision_requests) == 2:
            return tool_reply("artifact_write", {"name": "snapshot.md", "content": "# Revised\nRevenue was 42.\n"}, "revise")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Revised."}}]})

    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(peer))) as client:
        original, _ = finish(client, create_task(client, source))
        base = original["artifacts"][0]
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "Retitle the base as Revised.", "sources": ["facts.md"],
            "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        revised, _ = finish(client, response.json())
        assert revised["status"] == "completed", revised
        assert revised["id"] != original["id"]
        assert revised["base_artifact"]["id"] == base["id"]
        assert revised["base_artifact"]["sha256"] == base["sha256"]
        artifact = revised["artifacts"][0]
        assert artifact["parent_artifact_id"] == base["id"]
        assert artifact["version"] == 2
        assert client.get(artifact["download_url"]).content == b"# Revised\nRevenue was 42.\n"
        assert client.get(base["download_url"]).content == b"# Snapshot\nRevenue was 42.\n"
        base_message = next(message["content"] for message in revision_requests[0]["messages"]
                            if isinstance(message.get("content"), str) and message["content"].startswith("Pinned base artifact"))
        supplied_base = json.loads(base_message.split("\n", 1)[1])
        assert "text" not in supplied_base
        assert supplied_base["sha256"] == base["sha256"]
        assert client.get(f"/api/runs/{revised['id']}").json()["base_artifact"]["text"] == "# Snapshot\nRevenue was 42.\n"
        assert client.get(f"/api/runs/{original['id']}").json() == original

    with TestClient(create_app(store)) as client:
        saved = client.get(f"/api/runs/{revised['id']}").json()
        assert saved["base_artifact"]["id"] == base["id"]
        assert client.get(base["download_url"]).content == b"# Snapshot\nRevenue was 42.\n"


def test_revision_history_preserves_tool_pairs_and_selected_scope(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    (source / "private.md").write_bytes(b"UNSELECTED-SECRET-981\n")
    initial = ReportPeer()
    revision_requests: list[dict] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(message.get("content") == "Revise with history." for message in body["messages"]):
            revision_requests.append(body)
            if len(revision_requests) == 1:
                return tool_reply("plan_update", {"steps": ["Revise with history"]}, "revise-plan")
            return tool_reply("source_read", {"path": "private.md"}, "forbidden")
        return await initial(request)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        original, old_events = finish(client, create_task(client, source))
        base = original["artifacts"][0]
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "Revise with history.", "sources": ["facts.md"],
            "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        revised, events = finish(client, response.json())
        assert revised["error"]["code"] == "source_forbidden"
        messages = revision_requests[0]["messages"]
        assistant_calls = [call for message in messages for call in message.get("tool_calls", [])]
        results = [message for message in messages if message["role"] == "tool"]
        assert [call["function"]["name"] for call in assistant_calls] == ["plan_update", "workspace_list", "source_read", "artifact_write"]
        assert [call["id"] for call in assistant_calls] == [result["tool_call_id"] for result in results]
        assert json.loads(results[2]["content"])["text"] == "Revenue was 42.\n"
        assert "UNSELECTED-SECRET-981" not in json.dumps(messages)
        selected = next(event["data"] for event in events if event["kind"] == "context.selected")
        assert selected["history_run_ids"] == [original["id"]]
        assert client.get(f"/api/runs/{original['id']}/events").json()["events"] == old_events


def test_history_budget_omits_a_whole_turn_without_changing_saved_records(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    initial = ReportPeer()
    requests: list[dict] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(message.get("content") == "Revise under a small history budget." for message in body["messages"]):
            requests.append(body)
            if len(requests) == 1:
                return tool_reply("plan_update", {"steps": ["Write the revision"]}, "revision-plan")
            if len(requests) == 2:
                return tool_reply("artifact_write", {"name": "revision.md", "content": "# Revision\nRevenue was 42.\n"}, "revision")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved."}}]})
        return await initial(request)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        original, old_events = finish(client, create_task(client, source))
        base = original["artifacts"][0]
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "Revise under a small history budget.", "sources": ["facts.md"],
            "limits": {"max_history_bytes": 128},
            "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        revised, events = finish(client, response.json())
        assert revised["status"] == "completed", revised
        assert not any(message["role"] == "tool" for message in requests[0]["messages"])
        assert any(base["sha256"] in (message.get("content") or "") for message in requests[0]["messages"])
        selected = next(event["data"] for event in events if event["kind"] == "context.selected")
        assert selected["history_run_ids"] == []
        assert {"run_id": original["id"], "reason": "history_budget"} in selected["omitted"]
        assert client.get(f"/api/runs/{original['id']}/events").json()["events"] == old_events


def test_oversize_final_request_fails_before_model_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    peer = ReportPeer()
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source, task="Preserve this constraint: " + "x" * 6000,
                          limits={"context_window_tokens": 8192})
        final, events = finish(client, run)
        assert final["status"] == "failed"
        assert final["error"]["code"] == "context_limit_exceeded"
        assert peer.requests == []
        assert final["usage"]["model_calls"] == 0
        assert final["artifacts"] == []
        limited = next(event["data"] for event in events if event["kind"] == "context.limited")
        assert limited["output_token_reserve"] == 1024
        assert limited["capacity_tokens"] == 8192
        assert limited["upper_bound_tokens"] > 8192


def test_final_budget_drops_whole_history_but_keeps_base_and_current_constraint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    initial = ReportPeer()
    requests: list[dict] = []
    wire_bytes: list[int] = []
    constraint = "Revise exactly this base. Keep revenue 42, use title Revised."

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(message.get("content") == constraint for message in body["messages"]):
            requests.append(body)
            wire_bytes.append(len(request.content))
            if len(requests) == 1:
                return tool_reply("plan_update", {"steps": ["Write the revision"]}, "revision-plan")
            if len(requests) == 2:
                return tool_reply("artifact_write", {"name": "revision.md", "content": "# Revised\nRevenue was 42.\n"}, "revision")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved."}}]})
        return await initial(request)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        original, old_events = finish(client, create_task(client, source, task="Old instruction. " + "x" * 1500))
        base = original["artifacts"][0]
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": constraint, "sources": ["facts.md"], "limits": {"context_window_tokens": 7000},
            "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]},
        })
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())
        assert final["status"] == "completed", final["error"]
        assert requests[0]["messages"][-1]["content"] == constraint
        assert base["sha256"] in json.dumps(requests[0]["messages"])
        assert not any(message["role"] == "tool" for message in requests[0]["messages"])
        trimmed = next(event["data"] for event in events if event["kind"] == "context.pruned")
        assert trimmed["history_run_id"] == original["id"]
        assert trimmed["reason"] == "request_budget"
        budgets = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"]
        assert [budget["input_utf8_bytes"] for budget in budgets] == wire_bytes
        assert all(budget["upper_bound_tokens"] <= 7000 and budget["output_token_reserve"] == 1024 for budget in budgets)
        assert all(body["max_tokens"] == 1024 for body in requests)
        assert client.get(f"/api/runs/{original['id']}/events").json()["events"] == old_events


def test_large_current_tool_result_is_not_silently_cut_or_dispatched(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"A" * 20000)
    peer = ReportPeer()
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, limits={"context_window_tokens": 8192}))
        assert final["status"] == "failed"
        assert final["error"]["code"] == "context_limit_exceeded"
        assert len(peer.requests) == final["usage"]["model_calls"] == 3  # plan, catalog, read; the next request is too large
        assert any(event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read" for event in events)
        assert not any(event["kind"] == "tool.requested" and event["data"]["tool"] == "artifact_write" for event in events)
        assert final["artifacts"] == []


def test_base_reference_cannot_cross_session_or_override_verified_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(ReportPeer()))) as client:
        original, _ = finish(client, create_task(client, source))
        base = original["artifacts"][0]
        session = client.get("/api/sessions").json()[0]
        foreign = client.post("/api/sessions", json={"workspace_id": session["workspace_id"]}).json()
        body = {"task": "Revise.", "sources": ["facts.md"],
                "base_artifact": {"run_id": original["id"], "artifact_id": base["id"]}}
        response = client.post(f"/api/sessions/{foreign['id']}/runs", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_base_artifact"
        assert client.get(f"/api/sessions/{foreign['id']}/runs").json() == []
        body["base_artifact"]["text"] = "Injected replacement."
        assert client.post(f"/api/sessions/{session['id']}/runs", json=body).status_code == 422
        del body["base_artifact"]["text"]
        (store / "artifacts" / base["id"]).write_bytes(b"Externally changed.")
        response = client.post(f"/api/sessions/{session['id']}/runs", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "base_artifact_unavailable"
        assert len(client.get(f"/api/sessions/{session['id']}/runs").json()) == 1


def test_revisions_can_branch_from_an_explicit_older_version(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    initial = ReportPeer()
    bodies: list[dict] = []
    contents = {"second": "# Second\n42\n", "third": "# Third\n42\n", "branch": "# Branch\n42\n"}

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        users = [i for i, message in enumerate(body["messages"]) if message["role"] == "user"]
        task = body["messages"][users[-1]]["content"]
        if task not in contents:
            return await initial(request)
        bodies.append(body)
        results = [message for message in body["messages"][users[-1]+1:] if message["role"] == "tool"]
        if not results:
            return tool_reply("plan_update", {"steps": ["Revise the selected version"]}, f"plan-{task}")
        if len(results) == 1:
            return tool_reply("artifact_write", {"name": "snapshot.md", "content": contents[task]}, f"write-{task}")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        original, _ = finish(client, create_task(client, source))
        def revise(base_run: dict, task: str) -> dict:
            response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
                "task": task, "sources": ["facts.md"],
                "base_artifact": {"run_id": base_run["id"], "artifact_id": base_run["artifacts"][0]["id"]},
            })
            assert response.status_code == 202, response.text
            final, _ = finish(client, response.json())
            assert final["status"] == "completed", final["error"]
            return final
        second = revise(original, "second")
        third = revise(second, "third")
        branch = revise(original, "branch")
        assert [run["artifacts"][0]["version"] for run in [original, second, third, branch]] == [1, 2, 3, 2]
        assert third["artifacts"][0]["parent_artifact_id"] == second["artifacts"][0]["id"]
        assert branch["artifacts"][0]["parent_artifact_id"] == original["artifacts"][0]["id"]
        assert client.get(original["artifacts"][0]["download_url"]).content == b"# Snapshot\nRevenue was 42.\n"
        for body in bodies:
            calls = [call["id"] for message in body["messages"] for call in message.get("tool_calls", [])]
            results = [message["tool_call_id"] for message in body["messages"] if message["role"] == "tool"]
            assert len(calls) == len(set(calls))
            assert sorted(calls) == sorted(results)


def test_builtin_fixture_revision_starts_a_new_recipe_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store")) as client:
        original, _ = finish(client, create_task(client, source))
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "Save another fixture snapshot.", "sources": ["facts.md"],
            "base_artifact": {"run_id": original["id"], "artifact_id": original["artifacts"][0]["id"]},
        })
        assert response.status_code == 202, response.text
        revised, _ = finish(client, response.json())
        assert revised["status"] == "completed", revised["error"]
        assert revised["artifacts"][0]["version"] == 2
        assert client.get(original["artifacts"][0]["download_url"]).content == client.get(revised["artifacts"][0]["download_url"]).content


def test_budget_can_remove_a_completed_navigation_pair_without_cutting_source_result(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": [str(index) + "p" * 179 for index in range(6)]}, "plan")
        if len(requests) == 2:
            return tool_reply("source_read", {"path": "facts.md"}, "read")
        if len(requests) == 3:
            return tool_reply("artifact_write", {"name": "summary.md", "content": "Revenue was 42."}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, task="Keep the revenue fact.", limits={"context_window_tokens": 7000}))
        assert final["status"] == "completed", final["error"]
        trimmed = [event["data"] for event in events if event["kind"] == "context.pruned"]
        assert any(item.get("call_ids") == ["plan"] and item["reason"] == "completed_navigation" for item in trimmed)
        # The new result must first be delivered once; this synthetic envelope
        # then forces pruning only after that acknowledgement.
        assert any(message.get("tool_call_id") == "plan" for message in requests[1]["messages"])
        saved_state = next(message["content"] for body in requests for message in body["messages"]
                           if (message.get("content") or "").startswith("Saved executor state"))
        state = json.loads(saved_state.split("\n", 1)[1])
        assert state["plan_saved"] is True
        assert state["plan_steps"] == [str(index) + "p" * 179 for index in range(6)]
        source_results = [message for message in requests[2]["messages"] if message.get("tool_call_id") == "read"]
        assert json.loads(source_results[0]["content"])["text"] == "Revenue was 42.\n"
        assert any(message.get("content") == "Keep the revenue fact." for message in requests[2]["messages"])
        assert any(event["kind"] == "plan.updated" for event in events)


def qwen_metadata() -> dict:
    # Observed local /api/show profile, without weights or tokenizer vocabulary.
    return {"capabilities": ["completion", "tools"], "renderer": "qwen3.5", "parser": "qwen3.5",
            "template_source": "renderer_parser", "template": "{{ .Prompt }}",
            "model_info": {"general.architecture": "qwen35", "tokenizer.ggml.model": "gpt2",
                           "tokenizer.ggml.pre": "qwen35", "tokenizer.ggml.add_bos_token": False}}


def test_qwen_budget_verifies_renderer_profile_and_counts_tool_preamble(tmp_path: Path) -> None:
    from test_local_model import LocalPeer
    from agent_harness.model import ModelSettings
    peer = LocalPeer()
    async def qwen(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        return await peer(request)
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model",stream=False),
                               model_transport=httpx.MockTransport(qwen))) as client:
        config = client.get("/api/config").json()
        assert config["model"]["context_profile"] == "ollama-0.40.10-qwen35-gpt2"
        final, events = finish(client, create_task(client, source))
        assert final["status"] == "completed", final["error"]
        budgets = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"]
        assert all(budget["method"] == "qwen35_rendered_byte_bpe_upper_bound" for budget in budgets)
        assert all(budget["tool_preamble_bytes"] == 61 + 826 for budget in budgets)
        assert all(budget["upper_bound_tokens"] <= 8192 for budget in budgets)


@pytest.mark.parametrize("field,value", [
    ("system", "Hidden instructions."), ("messages", [{"role": "user", "content": "Hidden history."}]),
    ("renderer", "qwen3.8"), ("parser", "other"), ("template_source", "native_jinja"),
    ("template", "{{ .Messages }}"), ("tokenizer.ggml.model", "spm"),
    ("tokenizer.ggml.pre", "qwen2"), ("tokenizer.ggml.add_bos_token", True), ("version", "0.40.11"),
])
def test_unverified_qwen_profile_is_rejected_at_startup(tmp_path: Path, field: str, value: object) -> None:
    from test_local_model import LocalPeer
    from agent_harness.model import ModelSettings
    from agent_harness.contracts import RunFailure
    peer = LocalPeer()
    metadata = qwen_metadata()
    if field.startswith("tokenizer."):
        metadata["model_info"][field] = value
    elif field != "version":
        metadata[field] = value
    async def qwen(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": value if field == "version" else "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=metadata)
        return await peer(request)
    with pytest.raises(RunFailure) as error:
        with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model",stream=False),
                                   model_transport=httpx.MockTransport(qwen))):
            pass
    assert error.value.code == "context_profile_unsupported"
    assert peer.report.requests == []


@pytest.mark.parametrize("thinking", [False, True])
def test_qwen_render_bound_matches_independent_unicode_multicall_golden(tmp_path: Path, thinking: bool) -> None:
    from test_local_model import LocalPeer
    from agent_harness.model import ModelSettings
    peer = LocalPeer()
    requests: list[dict] = []
    async def qwen(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            metadata = qwen_metadata()
            if thinking:
                metadata["capabilities"].append("thinking")
                metadata["thinking"] = {"values": [False, True], "default": True}
            return httpx.Response(200, json=metadata)
        if request.url.path == "/api/tags":
            return await peer(request)
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": ["Prepare calibration."]}, "initial-plan")
        if len(requests) == 2:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [
                    {"id": "plan", "type": "function", "function": {"name": "plan_update", "arguments": json.dumps({"steps": ["檢查 <&>", "line\u2028break"]},ensure_ascii=False)}},
                    {"id": "catalog", "type": "function", "function": {"name": "workspace_list", "arguments": "{}"}},
                ]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Calibration ends without an artifact."}}]})
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model",stream=False,thinking=thinking),
                               model_transport=httpx.MockTransport(qwen))) as client:
        final, events = finish(client, create_task(client, source))
        assert final["error"]["code"] == "missing_artifact"
        assert len(requests) == 4  # Third decision ends without an artifact; fourth is publication-only recovery.
        assert [tool["function"]["name"] for tool in requests[3]["tools"]] == ["artifact_write"]
        assert any(event["kind"] == "publication.recovery_requested" for event in events)
        budgets = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"]
        fixture = json.loads((Path(__file__).parent / "fixtures/qwen35-rendered-navigation.json").read_text(encoding="utf-8"))
        # Worked oracle: one extra user wrapper (27 bytes), plus conservative
        # call separators (3 bytes). No production renderer/helper builds this fixture.
        segment = fixture["thinking_segment" if thinking else "segment"]
        expected_delta = len(segment.encode("utf-8")) + fixture["expected_upper_overcount_bytes"]
        assert requests[1]["tools"] == requests[2]["tools"]
        assert budgets[2]["rendered_prompt_bytes_upper_bound"] - budgets[1]["rendered_prompt_bytes_upper_bound"] == expected_delta


def test_unseen_navigation_result_is_protected_from_budget_pruning(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": [str(index) + "p" * 198 for index in range(8)]}, "plan")
        return tool_reply("artifact_write", {"name": "should-not-run.md", "content": "Unseen plan was dropped."}, "write")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, limits={"context_window_tokens": 7000}))
        assert final["error"]["code"] == "context_limit_exceeded"
        assert len(requests) == 1
        assert final["artifacts"] == []
        assert all(event["data"]["reason"] == "tool_descriptions"
                   for event in events if event["kind"] == "context.pruned")


def test_retained_executor_state_tracks_the_latest_successful_plan(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    requests: list[dict] = []
    replanned = False
    published = False
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal replanned, published
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": [str(index) + "p" * 179 for index in range(6)]}, "plan")
        if len(requests) == 2:
            return tool_reply("source_read", {"path": "facts.md"}, "read")
        has_retained = any((message.get("content") or "").startswith("Saved executor state") for message in body["messages"])
        if has_retained and not replanned:
            replanned = True
            return tool_reply("plan_update", {"steps": ["Use the verified revenue fact and finish."]}, "replanned")
        if not published:
            published = True
            return tool_reply("artifact_write", {"name": "summary.md", "content": "Revenue was 42."}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        final, _ = finish(client, create_task(client, source, task="Keep the revenue fact.", limits={"context_window_tokens": 7000}))
        assert final["status"] == "completed", final["error"]
        assert replanned
        last_state = next(message["content"] for message in requests[-1]["messages"]
                          if (message.get("content") or "").startswith("Saved executor state"))
        assert json.loads(last_state.split("\n",1)[1])["plan_steps"] == ["Use the verified revenue fact and finish."]


@pytest.mark.parametrize("selected,expected", [
    ("facts.md", {"plan_update", "workspace_list", "source_read", "artifact_write"}),
    ("metrics.CSV", {"plan_update", "workspace_list", "source_read", "artifact_write", "table_inspect", "table_aggregate", "number_calculate"}),
])
def test_model_tool_catalog_matches_selected_source_capabilities(tmp_path: Path, selected: str, expected: set[str]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    (source / "metrics.CSV").write_bytes(b"value\n42\n")
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return tool_reply("plan_update", {"steps": ["Inspect the selected source."]}, f"plan-{len(requests)}")
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=[selected], limits={"max_steps": 2}))
        assert final["error"]["code"] == "step_limit_exceeded"
        assert len(requests) == 2
        assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"plan_update"}
        assert any(event["kind"] == "plan.updated" for event in events)
        assert {tool["function"]["name"] for tool in requests[1]["tools"]} == expected

def recorded_publication_accepted(request: dict) -> bool:
    """True when the request's newest tool result is a successful artifact_write."""
    calls = {call['id']: call['function']['name'] for message in request['messages'] for call in message.get('tool_calls', [])}
    last = request['messages'][-1]
    if last.get('role') != 'tool' or calls.get(last.get('tool_call_id')) != 'artifact_write':
        return False
    return 'error' not in json.loads(last['content'])


@pytest.mark.parametrize('fixture_name', ['qwen35-analysis-context.json', 'qwen35-analysis-publication-context.json', 'qwen35-analysis-variant-publication.json', 'qwen35-analysis-missing-publication.json', 'qwen35-analysis-sample-contract-publication.json', 'qwen35-analysis-plan-catalog-publication.json'])
def test_acknowledged_analysis_evidence_can_fit_without_losing_latest_tool_result(tmp_path: Path, fixture_name: str) -> None:
    from test_local_model import LocalPeer
    from agent_harness.model import ModelSettings
    fixture = json.loads((Path(__file__).parent/'fixtures'/fixture_name).read_text(encoding='utf-8'))
    published = any(call['function']['name']=='artifact_write' for message in fixture['messages'] for call in message.get('tool_calls',[]))
    publish_index = next((index for index,message in enumerate(fixture['messages'])
                          if any(call['function']['name']=='artifact_write' for call in message.get('tool_calls',[]))),len(fixture['messages']))
    source = tmp_path/'source'
    source.mkdir()
    (source/'metrics.csv').write_text(fixture['source'],encoding='utf-8',newline='\n')
    metadata_peer = LocalPeer()
    requests = []
    oracle = fixture['oracle']
    original_citations = next((
        json.loads(call['function']['arguments']).get('citations', [])
        for message in fixture['messages'] for call in message.get('tool_calls', [])
        if call['function']['name'] == 'artifact_write'
    ), [])
    if oracle['exceptions'] is None:
        corrected_content = (
            "# 訂單分析報告\n\n| 項目 | 數值 | 來源 |\n|---|---|---|\n"
            f"| 訂單總數 | {oracle['orders']} | [metrics.csv E2] |\n"
            "| 異常總數 | 未提供，來源缺少異常欄位 | [metrics.csv E2] |\n"
            "| 整體異常率 | 未提供，缺少異常數 | [metrics.csv E2] |"
        )
        corrected_citations = original_citations or ['E2']
    else:
        corrected_content = (
            "# 訂單與異常分析報告\n\n| 指標 | 數值 | 來源 |\n|---|---|---|\n"
            f"| 訂單總數 | {oracle['orders']} | [metrics.csv E2] |\n"
            f"| 異常總數 | {oracle['exceptions']} | [metrics.csv E3] |\n"
            f"| 整體異常率 | {oracle['rate']}% | [metrics.csv E4] |"
        )
        corrected_citations = original_citations or ['E2', 'E3', 'E4']
    async def replay(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/version':
            return httpx.Response(200,json={'version':'0.40.10'})
        if request.url.path == '/api/show':
            return httpx.Response(200,json=qwen_metadata())
        if request.url.path != '/v1/chat/completions':
            return await metadata_peer(request)
        requests.append(json.loads(request.content))
        index=len(requests)-1
        if index<len(fixture['messages']):
            return httpx.Response(200,json={'choices':[{'message':fixture['messages'][index]}]})
        if index==len(fixture['messages']) and not recorded_publication_accepted(requests[-1]):
            # Only correct the recorded publication if the server rejected it.
            return tool_reply('artifact_write',{'name':'report.md','content':corrected_content,'citations':corrected_citations},'publish-corrected')
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Saved.'}}]})
    with TestClient(create_app(tmp_path/'store',model_settings=ModelSettings.local('contract-model',stream=False),model_transport=httpx.MockTransport(replay))) as client:
        final,events = finish(client,create_task(client,source,task=fixture['task'],sources=['metrics.csv']))
        assert final['status']=='completed',final['error']
        corrected=any(event['kind']=='tool.requested' and event['data']['call_id']=='publish-corrected' for event in events)
        assert len(requests)==len(fixture['messages'])+(2 if corrected else 1)
        saved_messages=[event['data']['message'] for event in events if event['kind']=='model.responded']
        assert saved_messages[:len(fixture['messages'])]==fixture['messages']
        saved_calls={call['id']:call for message in saved_messages for call in message.get('tool_calls',[])}
        artifact=client.get(final['artifacts'][0]['download_url']).text
        assert fixture['oracle']['orders'] in artifact
        if fixture['oracle']['rate'] is not None:
            assert fixture['oracle']['rate']+'%' in artifact
        for request in requests:
            calls=[call['id'] for message in request['messages'] for call in message.get('tool_calls',[])]
            results=[message['tool_call_id'] for message in request['messages'] if message['role']=='tool']
            assert calls==results
            assert all(call==saved_calls[call['id']] for message in request['messages'] for call in message.get('tool_calls',[]))
        before_publish=requests[publish_index]['messages']
        assert before_publish[-1]['role']=='tool' and json.loads(before_publish[-1]['content'])['evidence_id']==fixture['oracle']['last_evidence_id']
        final_request=requests[-1]['messages']
        snapshots=[message['content'] for message in final_request if (message.get('content') or '').startswith('Saved executor state')]
        snapshot=json.loads(snapshots[-1].split('\n',1)[1]) if snapshots else {}
        assert set(snapshot.get('evidence_sources',{})) <= set(snapshot.get('verified_evidence',{}))
        if fixture_name=='qwen35-analysis-variant-publication.json':
            assert snapshot.get('evidence_sources')
        ledger={}
        for identity,record in snapshot.get('verified_evidence',{}).items():
            record=dict(record)
            reference=record.pop('source_ref',None)
            ledger[identity]={**snapshot.get('evidence_sources',{}).get(reference,{}),**record}
        ledger.update({result['evidence_id']:result for message in final_request if message['role']=='tool'
                       for result in [json.loads(message['content'])] if 'evidence_id' in result})
        assert ledger['E2']['value']==fixture['oracle']['orders']
        if fixture['oracle']['exceptions'] is not None:
            assert ledger['E3']['value']==fixture['oracle']['exceptions']
        for event in events:
            if event['kind']=='tool.completed':
                result=event['data']['result']
                if result.get('evidence_id') in ledger:
                    # aggregatable_columns is a derived capability hint that may be
                    # omitted under budget pressure; the evidence itself must match.
                    hint='aggregatable_columns'
                    assert {k:v for k,v in ledger[result['evidence_id']].items() if k!=hint}=={k:v for k,v in result.items() if k!=hint}
        published_result=[event['data'] for event in events if event['kind']=='tool.completed' and event['data']['tool']=='artifact_write'][-1]
        assert len(final['artifacts'])==1
        assert final_request[-1]['role']=='tool'
        assert final_request[-1]['tool_call_id']==published_result['call_id']
        assert json.loads(final_request[-1]['content'])==published_result['result']
        if fixture_name=='qwen35-analysis-missing-publication.json':
            assert any(event['kind']=='context.pruned' and event['data']['reason']=='tool_narration' for event in events)
        if fixture_name=='qwen35-analysis-plan-catalog-publication.json':
            # Descriptions are omitted per request, only where the budget needs it;
            # names, schemas and the system rules are always kept.
            pruned_steps=[event['data']['step'] for event in events
                          if event['kind']=='context.pruned' and event['data']['reason']=='tool_descriptions']
            full_catalog=requests[1]['tools']
            for step in pruned_steps:
                request=requests[step-1]
                full={tool['function']['name']:tool for tool in full_catalog}
                for tool in request['tools']:
                    original=full[tool['function']['name']]['function']
                    assert tool['function']=={key:value for key,value in original.items() if key!='description'}
                assert request['messages'][0]==requests[1]['messages'][0]
        assert all(e['data']['context_budget']['upper_bound_tokens']<=8192 for e in events if e['kind']=='model.requested')


def test_pending_arithmetic_correction_and_latest_inspection_fit_without_becoming_success(tmp_path: Path) -> None:
    from test_local_model import LocalPeer
    from agent_harness.model import ModelSettings
    fixture = json.loads((Path(__file__).parent / 'fixtures/qwen35-arithmetic-correction-context.json').read_text(encoding='utf-8'))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'metrics.csv').write_text(fixture['source'], encoding='utf-8', newline='\n')
    metadata_peer = LocalPeer()
    requests = []
    async def replay(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/version':
            return httpx.Response(200, json={'version': '0.40.10'})
        if request.url.path == '/api/show':
            return httpx.Response(200, json=qwen_metadata())
        if request.url.path != '/v1/chat/completions':
            return await metadata_peer(request)
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        message = fixture['messages'][index] if index < len(fixture['messages']) else {'role': 'assistant', 'content': 'Cannot finish without correcting the calculation evidence.'}
        return httpx.Response(200, json={'choices': [{'message': message}]})
    with TestClient(create_app(tmp_path / 'store', model_settings=ModelSettings.local('contract-model', stream=False), model_transport=httpx.MockTransport(replay))) as client:
        run, events = finish(client, create_task(client, source, task=fixture['task'], sources=['metrics.csv']))
        assert run['status'] == 'failed' and run['error']['code'] == 'unresolved_tool_error', run['error']
        assert not run['artifacts']
        assert len(requests) == len(fixture['messages']) + 1
        failed_call = fixture['messages'][4]['tool_calls'][0]
        final_messages = requests[-1]['messages']
        assert any(failed_call in message.get('tool_calls', []) for message in final_messages)
        error = next(json.loads(message['content']) for message in final_messages if message.get('tool_call_id') == failed_call['id'])
        assert error['error']['code'] == 'invalid_evidence'
        latest = next(event['data'] for event in reversed(events) if event['kind'] == 'tool.completed')
        assert latest['result']['aggregatable_columns'] == ['week', 'orders']
        hints_pruned = any(event['kind'] == 'context.pruned' and event['data'].get('reason') == 'inspection_capability_hints'
                           for event in events)
        assert json.loads(final_messages[-1]['content']) == {
            key: value for key, value in latest['result'].items() if not (hints_pruned and key == 'aggregatable_columns')
        }
        guidance_pruned = any(event['kind'] == 'context.pruned' and event['data'].get('reason') == 'runtime_guidance_budget'
                              for event in events)
        saved_read = next(event['data']['result'] for event in events
                          if event['kind'] == 'tool.completed' and event['data']['tool'] == 'source_read')
        assert saved_read['runtime_guidance']['origin'] == 'agent_runtime'
        assert any('runtime_guidance' in str(message.get('content')) for message in final_messages) != guidance_pruned
        assert final_messages[-1]['tool_call_id'] == latest['call_id']
        assert all(event['data']['context_budget']['upper_bound_tokens'] <= 8192 for event in events if event['kind'] == 'model.requested')
