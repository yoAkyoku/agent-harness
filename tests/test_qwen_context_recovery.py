"""Replay a real Qwen missing-column correction trace at the workbench boundary."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.context import publication_finalization_prefix
from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_local_model import LocalPeer
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish


def test_publication_finalization_prefix_keeps_only_system_and_current_task() -> None:
    messages = [
        {"role": "system", "content": "completion instructions"},
        {"role": "user", "content": "earlier task and context"},
        {"role": "system", "content": "Saved executor state with verified source evidence"},
        {"role": "user", "content": "current task"},
    ]

    prefix = publication_finalization_prefix(messages, current_start=4)

    assert prefix == [messages[0], messages[-1]]
    assert prefix[0] is not messages[0]
    assert prefix[1] is not messages[-1]


def test_publication_finalization_compacts_to_task_and_file_summary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Status: regression tests pass.\n", encoding="utf-8")
    task = "請整理 facts.md 並保存為繁體中文摘要。"
    artifact = "# 狀態摘要\n\n" + "回歸測試已通過。[facts.md E1]\n" * 260
    replies = [
        {"calls": [{"name": "plan_update", "arguments": {"steps": ["讀取來源", "保存摘要"]}}]},
        {"calls": [{"name": "source_read", "arguments": {"path": "facts.md", "labels": []}}]},
        {"calls": [{"name": "artifact_write", "arguments": {
            "name": "summary.md", "content": artifact, "citations": ["E1"]}}]},
        {"calls": [{"name": "task_finish", "arguments": {"summary": "已保存狀態摘要。"}}]},
    ]
    metadata_peer = LocalPeer()
    requests: list[dict] = []

    async def replay(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        if request.url.path != "/v1/chat/completions":
            return await metadata_peer(request)
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        content = json.dumps(replies[index], ensure_ascii=False, separators=(",", ":"))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": content,
        }}]})

    settings = ModelSettings.local(
        "contract-model", stream=False, decision_protocol="json_tools", server_controlled_initial_plan=True,
    )
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(replay))) as client:
        run, events = finish(client, create_task(client, source, task=task, sources=["facts.md"]))
        assert run["status"] == "completed", (run["error"], [
            (event["kind"], event["data"].get("step"), event["data"].get("reason"),
             event["data"].get("upper_bound_tokens"))
            for event in events if event["kind"] in {"context.pruned", "context.limited"}
        ])
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(artifact)

    final_messages = requests[-1]["messages"]
    assert not any(message.get("role") == "tool" or message.get("tool_calls") for message in final_messages)
    assert len(final_messages) == 2
    assert all(event["data"]["context_budget"]["upper_bound_tokens"] <= 8192
               for event in events if event["kind"] == "model.requested")
    assert len(requests) == len(replies)


def test_revision_calculation_correction_fits_qwen_context_and_publishes_v2(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "metrics.csv").write_text(
        "week,orders,exceptions\n1,120,12\n2,150,9\n3,180,9\n", encoding="utf-8", newline="",
    )
    task = (
        "請分析 metrics.csv，計算訂單總數、異常總數及整體異常率（異常數 / 訂單數 × 100），"
        "保存為 report.md，附可追溯來源。若缺少必要欄位，明確指出缺少什麼；"
        "只計算有證據的數值，不能補零或猜測異常率。"
    )
    base_content = (
        "# metrics.csv 分析報告\n\n"
        "## 計算結果\n\n"
        "| 項目 | 數值 |\n| :--- | :--- |\n"
        "| 訂單總數 | 450 [metrics.csv E2] |\n"
        "| 異常總數 | 30 [metrics.csv E3] |\n"
        "| 整體異常率 | 6.67% [metrics.csv E4] |\n"
    )
    revised_content = (
        "# 三週訂單分析報告\n\n"
        "## 重點摘要\n三週共 450 筆訂單、30 件異常，整體異常率為 6.67%。"
        "[metrics.csv E3] [metrics.csv E4] [metrics.csv E5]\n\n"
        "| 指標 | 數值 |\n|---|---:|\n"
        "| 訂單總數 | 450 [metrics.csv E3] |\n"
        "| 異常總數 | 30 [metrics.csv E4] |\n"
        "| 整體異常率 | 6.67% [metrics.csv E5] |\n"
    )

    def call(name: str, arguments: dict) -> dict:
        return {"name": name, "arguments": arguments}

    def decision(*calls: dict) -> str:
        return json.dumps({"calls": list(calls)}, ensure_ascii=False, separators=(",", ":"))

    replies = [
        decision(call("plan_update", {"steps": ["檢查輸入", "依證據計算", "保存報告"]})),
        decision(call("workspace_list", {})),
        decision(call("table_inspect", {"path": "metrics.csv"})),
        decision(
            call("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
            call("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
        ),
        decision(call("number_calculate", {"operation": "ratio_percent", "numerator": "E3", "denominator": "E2"})),
        decision(call("artifact_write", {"name": "report.md", "content": base_content, "citations": ["E1", "E2", "E3", "E4"]})),
        json.dumps({"reply": "已保存 report.md。"}, ensure_ascii=False, separators=(",", ":")),
        decision(call("plan_update", {"steps": ["讀取 v1", "核對數值", "保存 v2"]})),
        decision(call("base_artifact_read", {})),
        decision(
            call("table_inspect", {"path": "metrics.csv"}),
            call("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
            call("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
            # E2 is the old v1 orders EID, not this Run's current numeric evidence.
            call("number_calculate", {"operation": "ratio_percent", "numerator": "E4", "denominator": "E2"}),
        ),
        decision(call("number_calculate", {"operation": "ratio_percent", "numerator": "E4", "denominator": "E3"})),
        decision(call("artifact_write", {"name": "report.md", "content": revised_content, "citations": ["E3", "E4", "E5"]})),
        json.dumps({"reply": "已更新 report.md。"}, ensure_ascii=False, separators=(",", ":")),
    ]
    metadata_peer = LocalPeer()
    requests: list[dict] = []

    async def replay(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        if request.url.path != "/v1/chat/completions":
            return await metadata_peer(request)
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        content = replies[index] if index < len(replies) else "{}"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": content,
        }}]})

    settings = ModelSettings.local(
        "contract-model", stream=False, decision_protocol="json_tools", server_controlled_initial_plan=True,
    )
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(replay))) as client:
        original, _ = finish(client, create_task(client, source, task=task, sources=["metrics.csv"]))
        assert original["status"] == "completed", original["error"]
        original_artifact = original["artifacts"][0]
        original_bytes = client.get(original_artifact["download_url"]).content
        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "請修改 report.md：使用繁體中文，改為簡潔的重點摘要和結果表格。保留原報告中的三項請求指標與來源證據。"
                    "請勿加入平均訂單數或其他未要求指標。不得變更數值：訂單總數 450、異常總數 30、整體異常率 6.67%。"
                    "保留每個數值旁的來源引用與 EID 對應附錄。以 v1 為底建立 report.md 新版本。",
            "sources": ["metrics.csv"],
            "base_artifact": {"run_id": original["id"], "artifact_id": original_artifact["id"]},
        })
        assert response.status_code == 202, response.text
        revised, events = finish(client, response.json())
        assert revised["status"] == "completed", (revised["error"], [
            (event["kind"], event["data"].get("step"), event["data"].get("reason"),
             event["data"].get("upper_bound_tokens"))
            for event in events if event["kind"] in {"context.pruned", "context.limited"}
        ])
        assert revised["artifacts"][0]["version"] == 2
        assert revised["artifacts"][0]["parent_artifact_id"] == original_artifact["id"]
        revised_text = client.get(revised["artifacts"][0]["download_url"]).text
        assert "450" in revised_text and "30" in revised_text and "6.67%" in revised_text
        assert client.get(original_artifact["download_url"]).content == original_bytes

    correction_request = requests[10]
    catalog_text = correction_request["messages"][0]["content"].split("Tool catalog:\n", 1)[1]
    assert [item["function"]["name"] for item in json.loads(catalog_text)] == ["number_calculate"]
    corrections = [event["data"] for event in events if event["kind"] == "correction.available"]
    assert len(corrections) == 1
    assert corrections[0]["error"]["code"] == "invalid_evidence"
    observed = next(event["data"] for event in events if event["kind"] == "correction.observed")
    assert observed["failed_call_ids"] == [corrections[0]["call_id"]]
    assert observed["tool"] == "number_calculate"
    assert any(event["kind"] == "context.pruned" and event["data"].get("reason") == "focused_correction_tools"
               for event in events)
    assert len(requests) == len(replies)
    assert all(event["data"]["context_budget"]["upper_bound_tokens"] <= 8192
               for event in events if event["kind"] == "model.requested")
