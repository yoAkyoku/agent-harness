import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import create_task, finish, tool_reply


def test_inspection_reports_full_column_aggregate_availability_without_computing_values(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"quantity,latency\n2,1\n5,2\n8,3\n9,broken\n")
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Inspect, aggregate the available quantity and publish"]}, "plan"),
        tool_reply("table_inspect", {"path": "facts.csv"}, "inspect"),
        tool_reply("table_aggregate", {"path": "facts.csv", "column": "quantity", "operation": "sum"}, "sum"),
        tool_reply("artifact_write", {"name": "report.md", "content": "Quantity total: 24 [facts.csv E2]",
                                     "citations": ["E1", "E2"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved."}, "finish"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert final["status"] == "completed", final["error"]
        inspected = next(e["data"]["result"] for e in events if e["kind"] == "tool.completed" and e["data"]["call_id"] == "inspect")
        assert inspected["aggregatable_columns"] == ["quantity"]
        assert inspected["sample"] == [["2", "1"], ["5", "2"], ["8", "3"]]
        assert "value" not in inspected and "operation" not in inspected
        assert json.loads(peer.requests[2]["messages"][-1]["content"]) == inspected
        summed = next(e["data"]["result"] for e in events if e["kind"] == "tool.completed" and e["data"]["call_id"] == "sum")
        assert summed["value"] == "24"
        assert "Quantity total: 24" in client.get(final["artifacts"][0]["download_url"]).text


@pytest.mark.parametrize("invalid", ["", "text", "NaN", "sNaN", "Infinity", "1e31", "1e-13", "1" * 65,
                                     "1e1000000", "-1e1000000", "1e-1000000", "1000000000000000000000000000000.1"])
def test_inspection_does_not_advertise_a_column_the_aggregate_tool_rejects(tmp_path: Path, invalid: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(("available,invalid\n2,1\n5,2\n8,3\n9," + invalid + "\n").encode())
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Inspect column availability"]}, "plan"),
        tool_reply("table_inspect", {"path": "facts.csv"}, "inspect"),
        tool_reply("table_aggregate", {"path": "facts.csv", "column": "invalid", "operation": "mean"}, "invalid"),
    ])
    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, create_task(client, source, sources=["facts.csv"], limits={"max_steps": 3}))
        assert final["status"] == "failed" and not final["artifacts"]
        inspected = next(e["data"]["result"] for e in events if e["kind"] == "tool.completed" and e["data"]["call_id"] == "inspect")
        assert inspected["aggregatable_columns"] == ["available"]
        rejected = next(e["data"] for e in events if e["kind"] == "tool.failed" and e["data"]["call_id"] == "invalid")
        assert rejected["code"] == "invalid_table"
        assert not any(e["kind"] == "tool.completed" and e["data"]["call_id"] == "invalid" for e in events)
