"""Invalid numeric operands must be repairable through the public correction contract."""
from pathlib import Path
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_workbench import create_task, finish


def test_both_invalid_operands_can_be_repaired_in_one_later_decision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes(b"orders,exceptions\n240,15\n")
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Aggregate columns and calculate rate."]}),
        json_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
        json_reply("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
        json_reply("number_calculate", {"operation": "ratio_percent", "numerator": "15", "denominator": "240"}),
        json_reply("number_calculate", {"operation": "ratio_percent", "numerator": "E2", "denominator": "E1"}),
        json_reply("artifact_write", {"name": "report.md", "content": "Rate: 6.25% [metrics.csv E3]", "citations": ["E3"]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools"),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=["metrics.csv"]))
        rejected = next(e["data"] for e in events if e["kind"] == "correction.available")
        assert rejected["error"]["correctable_fields"] == ["numerator", "denominator"]
        assert final["status"] == "completed", final["error"]
        observed = next(e["data"] for e in events if e["kind"] == "correction.observed")
        assert observed["failed_call_ids"] == [rejected["call_id"]]
        calculations = [e["data"] for e in events if e["kind"] == "tool.completed" and e["data"]["tool"] == "number_calculate"]
        assert len(calculations) == 1 and calculations[0]["result"]["value"] == "6.250000"
        assert observed["successful_call_id"] == calculations[0]["call_id"]
        assert client.get(final["artifacts"][0]["download_url"]).text.startswith("Rate: 6.25%")


@pytest.mark.parametrize("invalid", [
    ({"numerator": "15", "denominator": "240"}, {"numerator": "E3", "denominator": "E2"}),
    ({"numerator": "E99", "denominator": "E1"}, {}),
    ({"numerator": "15", "denominator": "E1"}, {"numerator": "E3"}),
    ({"numerator": "15", "denominator": "15"}, {}),
])
def test_operand_feedback_lists_confirmed_ids_and_unambiguous_literal_remaps(
    tmp_path: Path, invalid: tuple[dict, dict],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes(b"orders,exceptions\n240,15\n")
    arguments, expected_remap = invalid
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read, aggregate and calculate."]}),
        json_reply("source_read", {"path": "metrics.csv"}),
        json_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
        json_reply("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
        json_reply("number_calculate", {"operation": "ratio_percent", **arguments}),
        json_reply("number_calculate", {"operation": "ratio_percent", "numerator": "E3", "denominator": "E2"}),
        json_reply("artifact_write", {"name": "report.md", "content": "Rate: 6.25% [metrics.csv E4]", "citations": ["E4"]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools"),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=["metrics.csv"]))
        rejected = next(e["data"] for e in events if e["kind"] == "correction.available")
        message = rejected["error"]["message"]
        assert "Use numeric evidence IDs, not literal numbers." in message
        numeric_tail = message.split("Available numeric evidence: ", 1)[1]
        numeric_json, marker, remap_json = numeric_tail.partition(". Operand remap: ")
        numeric = json.loads(numeric_json)
        assert numeric == {
            "E2": {"path": "metrics.csv", "column": "orders", "operation": "sum", "value": "240"},
            "E3": {"path": "metrics.csv", "column": "exceptions", "operation": "sum", "value": "15"},
        }
        assert marker == (". Operand remap: " if expected_remap else "")
        remap = json.loads(remap_json) if marker else {}
        assert remap == expected_remap
        assert rejected["error"]["correctable_fields"] == ["numerator", "denominator"]
        assert final["status"] == "completed", final["error"]
        observed = next(e["data"] for e in events if e["kind"] == "correction.observed")
        assert observed["failed_call_ids"] == [rejected["call_id"]]


def test_fixing_bad_numerator_cannot_also_change_a_valid_denominator(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes(b"orders,exceptions\n240,15\n")
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Aggregate columns and calculate."]}),
        json_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
        json_reply("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
        json_reply("number_calculate", {"operation": "ratio_percent", "numerator": "15", "denominator": "E1"}),
        json_reply("number_calculate", {"operation": "ratio_percent", "numerator": "E2", "denominator": "E2"}),
        json_reply("artifact_write", {"name": "partial.md", "content": "An incomplete task [metrics.csv E3]", "citations": ["E3"]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved."}'}}]}),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools"),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=["metrics.csv"]))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert error["correctable_fields"] == ["numerator"]
        assert not any(e["kind"] == "correction.observed" for e in events)
        assert final["status"] == "failed"


def test_literal_operands_without_numeric_evidence_never_dispatch_or_invent_candidates(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_bytes(b"orders,exceptions\n240,15\n")
    bad = {"operation": "ratio_percent", "numerator": "15", "denominator": "240"}
    peer = CompletionPeer([
        json_reply("plan_update", {"steps": ["Read and calculate."]}),
        json_reply("source_read", {"path": "metrics.csv"}),
        json_reply("number_calculate", bad),
        json_reply("number_calculate", bad),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools"),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=["metrics.csv"], limits={"max_corrections": 1}))
        error = next(e["data"]["error"] for e in events if e["kind"] == "correction.available")
        assert json.loads(error["message"].split("Available numeric evidence: ", 1)[1]) == {}
        assert error["correctable_fields"] == ["numerator", "denominator"]
        assert not any(e["kind"] == "tool.dispatched" and e["data"]["tool"] == "number_calculate" for e in events)
        assert not final["artifacts"] and final["error"]["code"] == "correction_limit_exceeded"
