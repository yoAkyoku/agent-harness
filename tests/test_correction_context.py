"""A real Qwen trace (browser-01 B, 2026-11-18) replayed at the workbench boundary: the citation correction fits.

The model wrote a correct summary but left the facts from two sources uncited. The harness refused it
(`missing_body_citation`) and the request that carried the correction measured 8,402 tokens against the 8,192 bound,
so the Run failed. What filled it was not the 501-byte draft: the correction message spent about 700 bytes on two
SHA-256 hashes and a full citation table, and the model was still shown all seven tools. Now the message names each
uncited source with the form to cite it by, and only artifact_write is offered while the correction is pending.
"""
import json
import re
import shutil
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_local_model import LocalPeer
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish

ROOT = Path(__file__).resolve().parents[1]
TASK = "整理這個改版的目標、已知風險，以及三週訂單數的變化，存成一份附引用的摘要。"
DRAFT = ("# 訂單追蹤改版摘要\n\n## 目標\n讓客服在同一頁查看訂單進度、配送節點與異常紀錄。\n\n## 已知風險\n- 舊系統的配送狀態命名不一致。\n"
         "- 尚未核對所有歷史欄位。\n\n## 三週訂單數變化\n- 第一週：120 筆\n- 第二週：150 筆\n- 第三週：180 筆\n"
         "- 總訂單數：450 筆 [metrics.csv E4]\n- 異常訂單總數：30 筆 [metrics.csv E5]\n- 異常率：6.67% [E6]\n\n"
         "## 待確認事項\n- 退貨與補寄是否需要獨立狀態。\n")
CITED = (DRAFT.replace("異常紀錄。\n", "異常紀錄。[brief.md E1]\n").replace("歷史欄位。\n", "歷史欄位。[brief.md E1]\n")
         .replace("獨立狀態。\n", "獨立狀態。[notes.md E3]\n"))
# The model's decisions in the recorded Run, in order (its arguments verbatim), then the correction this test supplies.
RECORDED = [
    [("plan_update", {"steps": ["檢查輸入。", "依證據作答。", "核對成果。"]})],
    [("workspace_list", {})],
    [("source_read", {"path": "brief.md"}), ("source_read", {"path": "metrics.csv"}), ("source_read", {"path": "notes.md"})],
    [("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
     ("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
     ("number_calculate", {"operation": "ratio_percent", "numerator": "E3_sum_exceptions", "denominator": "E2_sum_orders"})],
    [("number_calculate", {"operation": "ratio_percent", "numerator": "E5", "denominator": "E4"})],
    [("artifact_write", {"name": "order_tracking_update_summary.md", "content": DRAFT, "citations": ["E1", "E2", "E3", "E4", "E5", "E6"]})],
]


def test_the_recorded_citation_correction_now_fits_in_8192_tokens(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(ROOT / "src" / "agent_harness" / "demo_sources", source)
    replies = [{"calls": [{"name": name, "arguments": arguments} for name, arguments in decision]} for decision in RECORDED]
    replies.append({"calls": [{"name": "artifact_write", "arguments": {
        "name": "order_tracking_update_summary.md", "content": CITED, "citations": ["E1", "E2", "E3", "E4", "E5", "E6"]}}]})
    replies.append({"reply": "已保存附引用的摘要。"})
    requests: list[dict] = []
    peer = LocalPeer()

    async def replay(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        if request.url.path != "/v1/chat/completions":
            return await peer(request)
        requests.append(json.loads(request.content))
        content = json.dumps(replies[len(requests) - 1], ensure_ascii=False, separators=(",", ":"))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})

    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools", server_controlled_initial_plan=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(replay))) as client:
        run, events = finish(client, create_task(client, source, task=TASK, sources=["brief.md", "metrics.csv", "notes.md"],
                                                 limits={"timeout_seconds": 600}))

    assert run["status"] == "completed", run["error"]
    assert not any(event["kind"] == "context.limited" for event in events)
    error = [event["data"]["error"] for event in events if event["kind"] == "correction.available"][-1]
    assert error["code"] == "missing_body_citation"
    assert "[brief.md E1]" in error["message"] and "[notes.md E3]" in error["message"]
    assert not re.search(r"[0-9a-f]{64}", error["message"])  # no full hashes the model cannot use
    correction = requests[6]  # the request that carried the refusal
    offered = correction["response_format"]["json_schema"]["schema"]["properties"]["calls"]["items"]["properties"]["name"]["enum"]
    assert offered == ["artifact_write"]
    assert any(event["kind"] == "context.pruned" and event["data"].get("tools") == ["artifact_write"] for event in events)
    budget = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"][6]
    print("correction request bound:", budget["upper_bound_tokens"], "of", budget["capacity_tokens"])
    assert budget["upper_bound_tokens"] <= budget["capacity_tokens"] == 8192  # was 8,402 in the recorded Run
