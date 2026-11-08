from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import create_task, finish, tool_reply


@pytest.mark.parametrize("task", ["請計算訂單總數並保存 report.md。", "請計算平均訂單並保存 report.md。"])
def test_tool_catalog_does_not_depend_on_task_wording(tmp_path: Path, task: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_text("week,orders\n7,60\n8,90\n", encoding="utf-8")
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["檢查欄位", "計算", "保存報告"]}, "plan"),
        tool_reply("table_inspect", {"path": "metrics.csv"}, "inspect"),
        tool_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}, "sum"),
        tool_reply("artifact_write", {
            "name": "report.md", "content": "訂單總數：150 [metrics.csv E2]", "citations": ["E1", "E2"],
        }, "write"),
        tool_reply("task_finish", {"summary": "已保存 report.md。"}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, server_controlled_initial_plan=True)

    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run, _ = finish(client, create_task(client, source, task=task, sources=["metrics.csv"]))
        assert run["status"] == "completed", run["error"]

    operations = next(
        item["function"]["parameters"]["properties"]["operation"]["enum"]
        for item in peer.requests[2]["tools"]
        if item["function"]["name"] == "table_aggregate"
    )
    assert operations == ["sum", "mean"]
