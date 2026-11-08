"""Multi-source body linkage through public HTTP/WS and model transport."""
from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_workbench import create_task, finish


@pytest.mark.parametrize("extra", ["", "\n[release.md E1]", "\nLiteral codes E10 and E20."], ids=["no-links", "one-source-only", "no-id-prefix-match"])
def test_recorded_index_without_body_links_is_rejected_then_model_repairs_content(tmp_path: Path, extra: str) -> None:
    root = Path(__file__).resolve().parents[1]
    saved = root / "tests/fixtures/recorded/9b-exact-receipt-batch-02/documents-standard"
    events = json.loads((saved / "events.json").read_bytes())
    prefix = []
    bad = None
    for event in events:
        if event["kind"] != "model.responded":
            continue
        decision = json.loads(event["data"]["message"]["wire_content"])
        calls = decision.get("calls", [])
        if calls and calls[0]["name"] == "artifact_write":
            bad = calls[0]["arguments"]
            break
        prefix.append(httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": event["data"]["message"]["wire_content"]}}]}))
    assert bad is not None
    bad = deepcopy(bad)
    bad["content"] += extra
    good = deepcopy(bad)
    good["content"] = good["content"].replace("all smoke tests pass)", "all smoke tests pass) [release.md E1]")
    good["content"] = good["content"].replace("事故升級聯絡人：Iris", "事故升級聯絡人：Iris [handoff.md E2]")
    good["content"] = good["content"].replace("Release checklist", "發布檢查清單")
    good["content"] = good["content"].replace("Support handoff", "支援交接")
    assert "[release.md E1]" in good["content"] and "[handoff.md E2]" in good["content"]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("release.md", "handoff.md"):
        (source / name).write_bytes((root / "evaluation/slice-09/documents-standard" / name).read_bytes())
    peer = CompletionPeer([*prefix, json_reply("artifact_write", bad), json_reply("artifact_write", good),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]})])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, saved_events = finish(client, create_task(client, source, sources=["release.md", "handoff.md"],
            task=json.loads((saved / "request.json").read_bytes())["task"]))
        rejected = [e for e in saved_events if e["kind"] == "correction.available"]
        assert len(rejected) == 1, [event["data"].get("error") for event in rejected]
        assert rejected[0]["data"]["error"]["code"] == "missing_body_citation"
        assert rejected[0]["data"]["error"]["correctable_fields"] == ["content"]
        assert run["status"] == "completed", run["error"]
        assert len(run["artifacts"]) == 1
        writers = [e["data"] for e in saved_events if e["kind"] == "tool.dispatched" and e["data"]["tool"] == "artifact_write"]
        assert len(writers) == 1 and writers[0]["arguments"]["content"].startswith(good["content"])
        assert any(e["kind"] == "correction.observed" for e in saved_events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(good["content"])


@pytest.mark.parametrize("body", ["Comparison: 50% [E3]", "Comparison: 50% [E3] from 6 [E1] and 12 [E2]"])
def test_body_reference_to_calculation_links_both_input_source_versions(tmp_path: Path, body: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.csv").write_bytes(b"count\n6\n")
    (source / "b.csv").write_bytes(b"count\n12\n")
    candidate = {"name": "comparison.md", "content": body, "citations": ["E3"]}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Aggregate both files and compare totals."]}),
        json_reply("table_aggregate", {"path": "a.csv", "column": "count", "operation": "sum"}),
        json_reply("table_aggregate", {"path": "b.csv", "column": "count", "operation": "sum"}),
        json_reply("number_calculate", {"operation": "ratio_percent", "numerator": "E1", "denominator": "E2"}),
        json_reply("artifact_write", candidate),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]})])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["a.csv", "b.csv"], task="Compare the source totals as a percentage."))
        assert run["status"] == "completed", run["error"]
        assert not any(e["kind"] == "correction.available" for e in events)
        text = client.get(run["artifacts"][0]["download_url"]).text
        assert text.startswith(candidate["content"])
        assert "ratio_percent(E1, E2) = 50.000000" in text
        assert "a.csv:L2-L2" in text and "b.csv:L2-L2" in text


def test_multisource_reference_inside_code_does_not_link_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("Owner: Mei\n", encoding="utf8")
    (source / "b.md").write_text("Owner: Bo\n", encoding="utf8")
    bad = {"name": "owners.md", "content": "Mei [E1]\n\n```text\n[E2] example\n```\nBo", "citations": ["E1", "E2"]}
    good = {**bad, "content": bad["content"] + " [E2]"}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and report both owners."]}),
        json_reply("source_read", {"path": "a.md"}), json_reply("source_read", {"path": "b.md"}),
        json_reply("artifact_write", bad), json_reply("artifact_write", good),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["a.md", "b.md"]))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert error["code"] == "missing_body_citation" and error["correctable_fields"] == ["content"]
        assert run["status"] == "completed", run["error"]
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(good["content"])


def test_multiple_evidence_ids_from_one_source_are_not_multiple_source_versions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"count\n6\n")
    candidate = {"name": "total.md", "content": "Total: 6", "citations": ["E1", "E2"]}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Inspect and total the source column."]}),
        json_reply("table_inspect", {"path": "facts.csv"}),
        json_reply("table_aggregate", {"path": "facts.csv", "column": "count", "operation": "sum"}),
        json_reply("artifact_write", candidate),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]})])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], task="Report the column total."))
        assert run["status"] == "completed", run["error"]
        assert not any(e["kind"] == "correction.available" for e in events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith("Total: 6")


def test_draft_body_evidence_must_be_in_declared_citations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders\n60\n90\n")
    draft = {"name": "report.md", "content": "Orders: 150 [E2]", "citations": ["E1"]}
    corrected = {**draft, "citations": ["E1", "E2"]}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and total orders."]}),
        json_reply("source_read", {"path": "facts.csv"}),
        json_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}),
        json_reply("artifact_draft", draft),
        json_reply("artifact_draft", corrected),
        httpx.Response(503, json={"diagnostic": "stop after draft"}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], publication_workflow="draft_review"))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert error["code"] == "invalid_evidence"
        assert error["correctable_fields"] == ["citations"]
        drafts = [e["data"]["result"] for e in events if e["kind"] == "tool.completed" and e["data"]["tool"] == "artifact_draft"]
        assert len(drafts) == 1
        assert "[E2] facts.csv" in drafts[0]["content"]
        assert "sum(orders) = 150" in drafts[0]["content"]
        assert run["status"] == "failed" and run["error"]["code"] == "model_unavailable"


@pytest.mark.parametrize("bad_content", [
    "訂單總數：150（來源 E1）",
    "| 指標 | 證據 ID |\n|---|---|\n| 訂單總數 | E1 |",
], ids=["prose-source-id", "evidence-table-cell"])
def test_bare_evidence_ids_in_artifact_body_are_rejected_and_repaired(tmp_path: Path, bad_content: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n60\n90\n", encoding="utf-8")
    bad = {"name": "report.md", "content": bad_content, "citations": ["E1"]}
    good = {"name": "report.md", "content": "訂單總數：150 [facts.csv E1]", "citations": ["E1"]}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["合計來源訂單數並引用結果。"]}),
        json_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}),
        json_reply("artifact_write", bad),
    ])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions" or len(peer.requests) < 3:
            return await peer(request)
        body = json.loads(request.content)
        peer.requests.append(body)
        latest = body["messages"][-1]
        try:
            projected = json.loads(latest["content"]) if latest["role"] == "user" else {}
        except json.JSONDecodeError:
            # Publication finalization ends the context with the user task text.
            projected = {}
        result = projected.get("tool_result", {}).get("value", {})
        if result.get("error", {}).get("code") == "missing_body_citation":
            return json_reply("artifact_write", good)
        return json_reply("task_finish", {"summary": "已保存。"})
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(transport))) as client:
        run, events = finish(client, create_task(
            client, source, task="請用繁體中文報告訂單總數並附來源。", sources=["facts.csv"],
        ))
        error = next(event["data"]["error"] for event in events if event["kind"] == "correction.available")
        assert error["code"] == "missing_body_citation"
        assert error["correctable_fields"] == ["content"]
        assert run["status"] == "completed", run["error"]
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(good["content"])


def test_unknown_body_id_is_corrected_and_known_body_ids_are_reconciled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders\n60\n90\n")
    bad = {"name": "report.md", "content": "Source [E99]; orders 150 [E2]", "citations": ["E1"]}
    # Body IDs that exist in this Run are reconciled into citations metadata,
    # so only the unknown body ID needs a model correction.
    fixed_content = {**bad, "content": "Source [E1]; orders 150 [E2]"}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and total orders."]}),
        json_reply("source_read", {"path": "facts.csv"}),
        json_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}),
        json_reply("artifact_write", bad), json_reply("artifact_write", fixed_content),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        errors = [e["data"]["error"] for e in events if e["kind"] == "correction.available"]
        assert [e["correctable_fields"] for e in errors] == [["content"]]
        assert run["status"] == "completed", run["error"]
        assert any(e["kind"] == "correction.observed" for e in events)
        assert "sum(orders) = 150" in client.get(run["artifacts"][0]["download_url"]).text


def test_source_snapshot_preserves_literal_bracketed_labels(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Service [E99] is an internal component label.\n", encoding="utf8")
    content = "Service [E99] is an internal component label.\n\nSource: [E1]"
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read the source and save a snapshot."]}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("artifact_write", {"name": "snapshot.md", "content": content, "citations": ["E1"]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"], task="Save a source snapshot."))
        assert run["status"] == "completed", run["error"]
        assert not any(e["kind"] == "correction.available" for e in events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(content)


def test_source_literal_label_does_not_authorize_another_body_reference(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Service [E99] is a component label.\n", encoding="utf8")
    bad = {"name": "report.md", "content": "Service [E99] is a component label.\nOwner: Mei [E99]", "citations": ["E1"]}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and report."]}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("artifact_write", bad), httpx.Response(503, json={"diagnostic": "stop"}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert error["code"] == "invalid_evidence" and error["correctable_fields"] == ["content"]
        assert not run["artifacts"]


def test_copied_source_line_does_not_hide_known_unbound_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Owner: Mei [E2]\n", encoding="utf8")
    content = "Owner: Mei [E2]"
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and report."]}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("artifact_write", {"name": "report.md", "content": content, "citations": ["E1"]}),
        json_reply("artifact_write", {"name": "report.md", "content": content, "citations": ["E1", "E2"]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert error["code"] == "invalid_evidence" and error["correctable_fields"] == ["citations"]
        assert run["status"] == "completed", run["error"]
        assert "[E2] facts.md" in client.get(run["artifacts"][0]["download_url"]).text


@pytest.mark.parametrize("body", ["Owner: Mei [E99]", "Unclosed `example\n\nOwner: Mei [E99]\n\nAnother ` marker"])
def test_unknown_body_citation_is_rejected_and_content_can_be_repaired(tmp_path: Path, body: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Owner: Mei\n", encoding="utf8")
    bad = {"name": "report.md", "content": body, "citations": ["E1"]}
    good = {**bad, "content": body.replace("[E99]", "[E1]")}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and report the owner."]}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("artifact_write", bad), json_reply("artifact_write", good),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert error["code"] == "invalid_evidence"
        assert error["correctable_fields"] == ["content"]
        assert run["status"] == "completed", run["error"]
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(good["content"])


@pytest.mark.parametrize("example", ["```text\n[E2] is an example label\n```", "Example: `[E2]` is literal text.",
                                     "Example: `literal\n[E2] label` is code."])
def test_code_example_is_not_a_live_evidence_reference(tmp_path: Path, example: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Owner: Mei\n", encoding="utf8")
    content = f"Owner: Mei [E1]\n\n{example}\n"
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read sources and publish."]}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("source_read", {"path": "facts.md"}),
        json_reply("artifact_write", {"name": "report.md", "content": content, "citations": ["E1"]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        assert not any(e["kind"] == "correction.available" for e in events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(content)


def test_exact_server_suffix_does_not_supply_missing_body_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_bytes(b"Owner: Mei\n")
    (source / "b.md").write_bytes(b"Owner: Lee\n")
    good = {"name": "owners.md", "content": "Mei [a.md E1]\nLee [b.md E2]", "citations": ["E1", "E2"]}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read both sources and draft their owners."]}),
        json_reply("source_read", {"path": "a.md"}), json_reply("source_read", {"path": "b.md"}),
        json_reply("artifact_draft", good), json_reply("artifact_draft", good),
        json_reply("artifact_draft", good), json_reply("artifact_write", good),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]})])
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 4:
            current = json.loads(json.loads(request.content)["messages"][-1]["content"])["tool_result"]["value"]["content"]
            suffix = current[current.index("\n\n## Verified tool evidence"):]
            peer.replies[4] = json_reply("artifact_draft", {**good, "content": "Mei and Lee." + suffix})
        return await peer(request)
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(transport))) as client:
        run, events = finish(client, create_task(client, source, sources=["a.md", "b.md"], publication_workflow="draft_review"))
        rejected = next(e for e in events if e["kind"] == "correction.available")
        assert rejected["data"]["error"]["code"] == "missing_body_citation"
        assert run["status"] == "completed", run["error"]
        text = client.get(run["artifacts"][0]["download_url"]).text
        assert text.startswith(good["content"]) and text.count("## Verified tool evidence") == 1
