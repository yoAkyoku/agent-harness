from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import create_task, finish, tool_reply


def test_calculated_values_must_cite_matching_calculation_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_text(
        "week,orders,exceptions\n1,120,8\n2,120,7\n", encoding="utf-8"
    )
    bad = (
        "訂單總數：240 [metrics.csv E1]\n"
        "異常總數：15 [metrics.csv E1]\n"
        "整體異常率：6.25% [metrics.csv E1]"
    )
    corrected = (
        "訂單總數：240 [metrics.csv E2]\n"
        "異常總數：15 [metrics.csv E3]\n"
        "整體異常率：6.25% [metrics.csv E4]"
    )
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["檢查欄位", "計算指定指標", "保存報告"]}, "plan"),
        tool_reply("table_inspect", {"path": "metrics.csv"}, "inspect"),
        tool_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}, "orders-sum"),
        tool_reply("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}, "exceptions-sum"),
        tool_reply("number_calculate", {"operation": "ratio_percent", "numerator": "E3", "denominator": "E2"}, "rate"),
        tool_reply("artifact_write", {"name": "report.md", "content": bad, "citations": ["E1"]}, "bad-write"),
        tool_reply("artifact_write", {"name": "report.md", "content": corrected, "citations": ["E2", "E3", "E4"]}, "good-write"),
        tool_reply("task_finish", {"summary": "已保存 report.md。"}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, server_controlled_initial_plan=True)

    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(
            client, source,
            task="請計算訂單總數、異常總數及整體異常率，保存 report.md 並附來源。",
            sources=["metrics.csv"],
        ))
        assert run["status"] == "completed", run["error"]
        artifact = client.get(run["artifacts"][-1]["download_url"]).text

    correction = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert correction["error"]["code"] == "missing_calculation_citation"
    assert "E2" in correction["error"]["message"]
    assert artifact.startswith(corrected)
    system_prompt = peer.requests[0]["messages"][0]["content"]
    assert "引用產生該值的計算 EID" in system_prompt
