"""Evidence provenance at public workbench and external model transport seams."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_workbench import create_task, finish, tool_reply


def test_copied_evidence_section_is_rejected_and_model_can_repair_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    bad = {"name":"owner.md","content":"Owner: Mei [facts.md E1]\n\n## Verified tool evidence\n- [E1] old.csv:L2-L3 SHA-256 " + "0"*64 + "\n","citations":["E1"]}
    good = {"name":"owner.md","content":"Owner: Mei [facts.md E1]","citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read and save owner"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),json_reply("artifact_draft",bad),
        json_reply("artifact_draft",good),json_reply("artifact_write",good),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 3:
            body = json.loads(request.content)
            error = json.loads(body["messages"][-1]["content"])["tool_result"]["value"]["error"]
            assert error["code"] == "evidence_section_mismatch"
            assert error["correctable_fields"] == ["content"]
        return await peer(request)
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        run,events = finish(client,create_task(client,source,publication_workflow="draft_review"))
        assert run["status"] == "completed",run["error"]
        drafts = [e for e in events if e["kind"] == "tool.dispatched" and e["data"]["tool"] == "artifact_draft"]
        assert len(drafts) == 1
        assert "old.csv" not in drafts[0]["data"]["arguments"]["content"]
        assert any(e["kind"] == "correction.observed" for e in events)
        saved = client.get(run["artifacts"][0]["download_url"]).text
        assert saved.startswith(good["content"])
        assert saved.count("## Verified tool evidence") == 1
        assert "old.csv" not in saved


def test_literal_evidence_heading_examples_and_general_e_labels_remain_valid(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    content = ("Owner: Mei [facts.md E1]\nLiteral labels: E2 and E100 are example names.\n\n"
               "> ## Verified tool evidence\n\n"
               "```markdown\n## Verified tool evidence\n- [E99] quoted example, not current evidence\n```\n\n"
               "~~~~md\n## Verified tool evidence\n~~~~\n\n"
               "    ## Verified tool evidence\n"
               "Inline Unicode separator\u2028## Verified tool evidence\n")
    candidate = {"name":"examples.md","content":content,"citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read and save examples"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),json_reply("artifact_write",candidate),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(peer))) as client:
        run,events = finish(client,create_task(client,source))
        assert run["status"] == "completed",run["error"]
        saved = client.get(run["artifacts"][0]["download_url"]).text
        assert saved.startswith(content)
        assert saved.count("## Verified tool evidence") == 6
        assert not any(e["kind"] == "correction.available" for e in events)


def test_copied_snapshot_appendix_feedback_identifies_current_artifact_citation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    store = tmp_path / "store"
    seed = [tool_reply("artifact_write", {"name":"owner.md","content":"Owner: Mei\n"}),
            httpx.Response(200,json={"choices":[{"message":{"role":"assistant","content":"Seeded."}}]})]
    async def seed_transport(request: httpx.Request) -> httpx.Response:
        return seed.pop(0)
    with TestClient(create_app(store,model_transport=httpx.MockTransport(seed_transport))) as client:
        original,_ = finish(client,create_task(client,source))
        base = original["artifacts"][0]
        old_bytes = client.get(base["download_url"]).content
    bad = {"name":"owner.md","content":"Owner: Mei E2\n\n## Verified tool evidence\n- [E1] facts.md:L1-L1 SHA-256 " + "0"*64,"citations":["E1"]}
    good = {"name":"owner.md","content":"Owner: Mei [owner.md E1]","citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read base and reformat"]}),json_reply("base_artifact_read",{}),
        json_reply("artifact_draft",bad),json_reply("artifact_draft",good),json_reply("artifact_write",good),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 3:
            error = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]["error"]
            assert "[owner.md E1]" in error["message"]
            assert "pinned_artifact" in error["message"]
            assert error["correctable_fields"] == ["content"]
        return await peer(request)
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(store,model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        response = client.post(f"/api/sessions/{original['session_id']}/runs",json={"task":"Reformat the existing owner statement.",
            "sources":["facts.md"],"publication_workflow":"draft_review","base_artifact":{"run_id":original["id"],"artifact_id":base["id"]}})
        assert response.status_code == 202,response.text
        final,events = finish(client,response.json())
        assert final["status"] == "completed",final["error"]
        assert final["artifacts"][0]["version"] == 2
        assert client.get(base["download_url"]).content == old_bytes
        saved = client.get(final["artifacts"][0]["download_url"]).text
        assert saved.startswith(good["content"])
        assert "artifact snapshot, not a fresh source calculation" in saved
        assert any(e["kind"] == "correction.observed" for e in events)


@pytest.mark.parametrize("prefix", ["## Verified tool evidence\n- [E1] forged.csv\n\n",
    "## Verified tool evidence\n- [E1] forged.csv\n\n```\n", "```\n"], ids=["extra-section","masked-suffix","no-active-heading"])
def test_valid_current_suffix_does_not_hide_an_extra_forged_section(tmp_path: Path, prefix: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    good = {"name":"owner.md","content":"Owner: Mei [facts.md E1]","citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read and save"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),json_reply("artifact_draft",good),
        json_reply("artifact_draft",good),json_reply("artifact_draft",good),json_reply("artifact_write",good),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    current = ""
    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal current
        if request.url.path == "/v1/chat/completions":
            if len(peer.requests) == 3:
                current = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]["content"]
                peer.replies[3] = json_reply("artifact_draft", {**good,"content":prefix+current})
            elif len(peer.requests) == 4:
                error = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]["error"]
                assert error["code"] == "evidence_section_mismatch"
                peer.replies[4] = json_reply("artifact_draft", {**good,"content":current})
        return await peer(request)
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        run,events = finish(client,create_task(client,source,publication_workflow="draft_review"))
        assert run["status"] == "completed",run["error"]
        saved = client.get(run["artifacts"][0]["download_url"]).text
        assert saved == current
        assert saved.count("## Verified tool evidence") == 1 and "forged.csv" not in saved
        assert len([e for e in events if e["kind"] == "tool.dispatched" and e["data"]["tool"] == "artifact_draft"]) == 2


def test_citation_retry_preserves_content_before_separate_section_repair(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    content = "Owner: Mei [facts.md E2]\n\n## Verified tool evidence\n- [E2] historical.csv\n"
    bad = {"name":"owner.md","content":content,"citations":["E2"]}
    cite_retry = {**bad,"citations":["E1"]}
    good = {"name":"owner.md","content":"Owner: Mei [facts.md E1]","citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read and publish"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),json_reply("artifact_draft",bad),
        json_reply("artifact_draft",cite_retry),json_reply("artifact_draft",good),json_reply("artifact_write",good),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) in {3,4}:
            error = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]["error"]
            assert "Respect correctable_fields" in error["message"]
            if len(peer.requests) == 3:
                assert error["code"] == "invalid_evidence" and error["correctable_fields"] == ["citations"]
            else:
                assert error["code"] == "evidence_section_mismatch" and error["correctable_fields"] == ["content"]
        return await peer(request)
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        run,events = finish(client,create_task(client,source,publication_workflow="draft_review"))
        assert run["status"] == "completed",run["error"]
        assert len([e for e in events if e["kind"] == "correction.available"]) == 2
        assert any(e["kind"] == "correction.observed" for e in events)
        saved = client.get(run["artifacts"][0]["download_url"]).text
        assert saved.startswith(good["content"]) and "historical.csv" not in saved


def test_direct_citation_retry_does_not_recommend_an_unavailable_draft_tool(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    body = "Owner: Mei [facts.md E1]"
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read and publish"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),
        json_reply("artifact_write", {"name":"owner.md","content":body,"citations":["E9"]}),
        json_reply("artifact_write", {"name":"owner.md","content":body,"citations":["E1"]}),
        httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"reply":"Saved."}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 3:
            error = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]["error"]
            assert error["correctable_fields"] == ["citations"]
            assert "artifact_draft" not in error["message"]
        return await peer(request)
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        run,events = finish(client,create_task(client,source))
        assert run["status"] == "completed",run["error"]
        assert any(e["kind"] == "correction.observed" for e in events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(body)


def test_sha_publication_cannot_offer_content_edits_for_a_different_citation_set(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    (source / "other.md").write_bytes(b"Owner: Bo\n")
    candidate = {"name":"owner.md","content":"Owner: Mei [facts.md E1]","citations":["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps":["Read sources and stage owner"]}),
        json_reply("source_read", {"path":"facts.md","labels":[]}),json_reply("source_read", {"path":"other.md","labels":[]}),
        json_reply("artifact_draft",candidate),json_reply("artifact_write", {"name":"owner.md","draft_sha256":"0"*64,"citations":["E2"]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 4:
            staged = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]
            peer.replies[4] = json_reply("artifact_write", {"name":"owner.md","draft_sha256":staged["sha256"],"citations":["E2"]})
        if request.url.path == "/v1/chat/completions" and len(peer.requests) >= 5:
            return httpx.Response(503)
        return await peer(request)
    settings = ModelSettings.local("contract-model",stream=False,decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store",model_settings=settings,model_transport=httpx.MockTransport(transport))) as client:
        run,events = finish(client,create_task(client,source,sources=["facts.md","other.md"],publication_workflow="draft_review",require_publish_approval=True))
        assert run["status"] == "failed"
        assert run["error"]["code"] == "unreviewed_artifact"
        assert not run["approvals"] and not run["artifacts"]
        assert not any(e["kind"] == "correction.available" for e in events)
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "artifact_write" for e in events)
