"""Public workbench controls with a real installed vocab-only tokenizer, no generation."""
import json
import os
from pathlib import Path
import shutil
import asyncio
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish
from test_workbench import tool_reply
from test_approval import pending_approval
from test_revision_draft_context import recorded_replies

# Opt-in: point these at an installed Ollama libllama.dll and the Qwen3.5 9B GGUF blob
# (blob sha256-02d45dc1cf451ba2475ac33b301c2dd8f985abe4c182ce04a1f2f5bf0260278d). Nothing is downloaded.
LIBRARY = Path(os.environ.get("HARNESS_TEST_TOKENIZER_LIBRARY", ""))
MODEL = Path(os.environ.get("HARNESS_TEST_TOKENIZER_MODEL", ""))
NAME = "qwen3.5:9b-q4_K_M"
DIGEST = "56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90"
pytestmark = pytest.mark.skipif(os.name != "nt" or not LIBRARY.is_file() or not MODEL.is_file(),
                                reason="Requires the pinned installed Windows Qwen35 vocab assets, never downloads them.")


def test_exact_count_admits_full_long_task_and_preserves_real_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    task = ("Read facts and save owner. " + "x " * 2400).strip()
    candidate = {"name":"owner.md","content":"Owner: Mei [facts.md E1]","citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read and save owner"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),
        json_reply("artifact_draft", candidate), json_reply("artifact_write", candidate),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200,json={"version":"0.40.10"})
        if request.url.path == "/api/tags":
            return httpx.Response(200,json={"models":[{"name":NAME,"digest":DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200,json=qwen_metadata())
        return await peer(request)
    settings = ModelSettings.local(NAME,stream=False,decision_protocol="json_tools",
        exact_tokenizer_library=LIBRARY,exact_tokenizer_model=MODEL)
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        run, events = finish(client,create_task(client,source,task=task,publication_workflow="draft_review"))
        assert run["status"] == "completed",run["error"]
        requested = [e["data"]["context_budget"] for e in events if e["kind"] == "model.requested"]
        assert len(requested) == 5
        assert all(b["method"] == "qwen35_exact_vocab_only" and b["upper_bound_tokens"] <= 8192 for b in requested)
        assert all(b["upper_bound_tokens"] == b["input_tokens"] + 1024 for b in requested)
        assert any(m["content"] == task for m in peer.requests[0]["messages"])
        assert not any(e["kind"] == "context.pruned" for e in events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(candidate["content"])


def test_exact_count_rejects_actual_token_overflow_before_generation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    generations = []
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200,json={"version":"0.40.10"})
        if request.url.path == "/api/tags":
            return httpx.Response(200,json={"models":[{"name":NAME,"digest":DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200,json=qwen_metadata())
        generations.append(json.loads(request.content))
        return httpx.Response(503)
    settings = ModelSettings.local(NAME,stream=False,decision_protocol="json_tools",
        exact_tokenizer_library=LIBRARY,exact_tokenizer_model=MODEL)
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        task = "Read facts. " + "".join(chr(0x4E00+i) for i in range(7000))
        run, events = finish(client,create_task(client,source,task=task))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "context_limit_exceeded"
        limited = next(e["data"] for e in events if e["kind"] == "context.limited")
        assert limited["method"] == "qwen35_exact_vocab_only"
        assert limited["upper_bound_tokens"] > 8192
        assert limited["output_token_reserve"] == 1024
        assert not generations and not run["artifacts"]


def test_changed_verified_asset_stops_run_without_generation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    copy = tmp_path / "libllama.dll"
    shutil.copyfile(LIBRARY,copy)
    generations = []
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200,json={"version":"0.40.10"})
        if request.url.path == "/api/tags":
            return httpx.Response(200,json={"models":[{"name":NAME,"digest":DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200,json=qwen_metadata())
        generations.append(json.loads(request.content))
        return httpx.Response(503)
    settings = ModelSettings.local(NAME,stream=False,decision_protocol="json_tools",
        exact_tokenizer_library=copy,exact_tokenizer_model=MODEL)
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        with copy.open("ab") as stream:
            stream.write(b"changed")
        run, _ = finish(client,create_task(client,source))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "exact_context_unavailable"
        assert not generations and run["usage"]["model_calls"] == 0


def test_recorded_revision_prefix_can_finish_approved_second_version_with_complete_context(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    saved = root / "tests/fixtures/recorded/gui-real-9b-revision-04"
    manifest = json.loads((saved / "manifest.json").read_bytes())
    original = (saved / "failed/ba2622198f7d438d8888b871579b169e/artifacts/e9a40ac96973a6df02f66d94dc0fe3ec/report.md").read_bytes()
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes((root / "evaluation/slice-09/analysis-standard/metrics.csv").read_bytes())
    replies = [tool_reply("artifact_write", {"name":"report.md","content":original.decode()}),
               httpx.Response(200,json={"choices":[{"message":{"role":"assistant","content":"Seeded."}}]})]
    async def seed_transport(request: httpx.Request) -> httpx.Response:
        return replies.pop(0)
    store = tmp_path / "store"
    with TestClient(create_app(store,model_transport=httpx.MockTransport(seed_transport))) as client:
        seeded,_ = finish(client,create_task(client,source,sources=["metrics.csv"]))
        base = seeded["artifacts"][0]
        assert base["sha256"] == manifest["base"]["sha256"]
    content = "| 項目 | 數值 |\n|---|---|\n| 訂單總數 | 240 [metrics.csv E3] |\n| 異常總數 | 15 [metrics.csv E4] |\n| 異常率 | 6.25% [E5] |"
    peer = CompletionPeer([*recorded_replies(saved / "failed/56ed23d8ea34460ba1f1a6c41697887a/events.json"),
        json_reply("number_calculate", {"operation":"ratio_percent","numerator":"E4","denominator":"E3"}),
        json_reply("artifact_draft", {"name":"report.md","content":content,"citations":["E3","E4","E5"]}),
        json_reply("artifact_write", {"name":"report.md","draft_sha256":"0"*64,"citations":["E3","E4","E5"]}),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":{"artifact_ids":[]}}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200,json={"version":"0.40.10"})
        if request.url.path == "/api/tags":
            return httpx.Response(200,json={"models":[{"name":NAME,"digest":DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200,json=qwen_metadata())
        response = await peer(request)
        decision = json.loads(response.json()["choices"][0]["message"]["content"])
        if decision.get("calls",[{}])[0].get("arguments",{}).get("draft_sha256") == "0"*64:
            ack = json.loads(peer.requests[-1]["messages"][-1]["content"])["tool_result"]["value"]
            decision["calls"][0]["arguments"]["draft_sha256"] = ack["sha256"]
        elif "reply" in decision:
            ack = json.loads(peer.requests[-1]["messages"][-1]["content"])["tool_result"]["value"]
            decision["reply"]["artifact_ids"] = [ack["id"]]
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":json.dumps(decision,ensure_ascii=False)}}]})
    settings = ModelSettings.local(NAME,stream=False,decision_protocol="json_tools",
        exact_tokenizer_library=LIBRARY,exact_tokenizer_model=MODEL)
    with TestClient(create_app(store,model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        response = client.post(f"/api/sessions/{seeded['session_id']}/runs",json={"task":manifest["task"],"sources":["metrics.csv"],
            "limits":manifest["limits"],"publication_workflow":"draft_review","completion_workflow":"artifact_receipt",
            "require_publish_approval":True,"base_artifact":{"run_id":seeded["id"],"artifact_id":base["id"]}})
        assert response.status_code == 202,response.text
        run = response.json()
        approval = pending_approval(client,run)
        assert approval["arguments"]["content"].startswith(content)
        client.post(f"/api/runs/{run['id']}/approvals/{approval['id']}",json={"decision":"approve","parameters_sha256":approval["parameters_sha256"]}).raise_for_status()
        final,events = finish(client,run)
        assert final["status"] == "completed",final["error"]
        assert final["artifacts"][0]["version"] == 2
        assert client.get(base["download_url"]).content == original
        assert client.get(final["artifacts"][0]["download_url"]).text == approval["arguments"]["content"]
        assert len(peer.requests) == 8
        assert not any(e["kind"] == "context.pruned" for e in events)
        assert all(e["data"]["context_budget"]["upper_bound_tokens"] <=8192 for e in events if e["kind"] == "model.requested")


def test_exact_count_waits_for_shared_capacity_and_cancel_does_not_admit_generation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    four_started = Event()
    active = 0
    maximum = 0
    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal active,maximum
        if request.url.path == "/api/version":
            return httpx.Response(200,json={"version":"0.40.10"})
        if request.url.path == "/api/tags":
            return httpx.Response(200,json={"models":[{"name":NAME,"digest":DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200,json=qwen_metadata())
        active += 1
        maximum = max(maximum,active)
        if active == 4:
            four_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
        return httpx.Response(503)
    settings = ModelSettings.local(NAME,stream=False,decision_protocol="json_tools",
        exact_tokenizer_library=LIBRARY,exact_tokenizer_model=MODEL)
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        blockers = [create_task(client,source) for _ in range(4)]
        assert four_started.wait(15)
        waiting = create_task(client,source)
        with client.websocket_connect(f"/api/runs/{waiting['id']}/stream") as stream:
            while stream.receive_json()["kind"] != "run.started":
                pass
            assert client.post(f"/api/runs/{waiting['id']}/cancel").status_code == 202
        final,events = finish(client,waiting)
        assert final["status"] == "cancelled"
        assert not any(e["kind"] == "model.requested" for e in events)
        assert final["usage"]["model_calls"] == 0
        for run in blockers:
            client.post(f"/api/runs/{run['id']}/cancel").raise_for_status()
            assert finish(client,run)[0]["status"] == "cancelled"
        assert maximum == 4


def test_exact_profile_survives_real_restart_and_resumes_confirmed_publication(tmp_path: Path) -> None:
    from draft_process import running_draft_service, wait_draft_tool, finish_draft_run
    from test_recovery import create_process_task, stop
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    candidate = {"name":"snapshot.md","content":"Revenue was 42. [facts.md E1]","citations":["E1"]}
    replies = [json_reply("plan_update", {"steps":["Read and publish"]}),
               json_reply("source_read", {"path":"facts.md","labels":[]}),json_reply("artifact_write",candidate)]
    store = tmp_path / "store"
    with running_draft_service(store,replies,decision_protocol="json_tools",exact_assets=(LIBRARY,MODEL)) as first:
        original = create_process_task(first.client,source,completion_workflow="artifact_receipt")
        wait_draft_tool(first.client,original["id"],"tool.completed","artifact_write")
        published = first.client.get(f"/api/runs/{original['id']}").json()["artifacts"][0]
        stop(first.process)
    reply = httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant",
        "content":json.dumps({"reply":{"artifact_ids":[published["id"]]}})}}]})
    with running_draft_service(store,[reply],decision_protocol="json_tools",exact_assets=(LIBRARY,MODEL)) as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        assert checkpoint["model"]["context_profile"] == "ollama-0.40.10-qwen35-exact-vocab-only"
        response = second.client.post(f"/api/runs/{original['id']}/resume",json={
            "checkpoint_sha256":checkpoint["sha256"],"submission_key":"exact-profile-resume"})
        assert response.status_code == 202,response.text
        final,events = finish_draft_run(second.client,response.json()["id"])
        assert final["status"] == "completed",final["error"]
        assert final["result"] == "已發布：snapshot.md。"
        assert final["inherited_artifacts"][0]["id"] == published["id"]
        assert [e["data"]["tool"] for e in events if e["kind"] == "tool.dispatched"] == ["task_finish"]
        assert second.client.get(published["download_url"]).text.startswith(candidate["content"])
