"""A completion reply must describe the published artifact, not restate the task."""
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.verifiers import completion_summary
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_completion_grounding import json_decision_reply
from test_workbench import create_task, finish

MISSING_REPORT = (
    "# 訂單分析\n\n| 項目 | 數值 |\n|---|---|\n"
    "| 訂單總數 | 150 [metrics.csv E2] |\n"
    "| 異常總數 | 未提供 |\n"
    "| 整體異常率 | 無法計算 |\n"
)


def test_summary_claiming_an_item_the_artifact_marks_missing_is_rejected() -> None:
    failure = completion_summary("已發布 report.md，包含訂單總數、異常總數及整體異常率。", [MISSING_REPORT])
    assert failure is not None and failure.code == "unsupported_completion_claim"
    assert failure.correctable_fields == ("summary",)
    assert "異常總數" in failure.message and "整體異常率" in failure.message


def test_summary_that_reports_the_missing_item_as_missing_is_accepted() -> None:
    assert completion_summary("已發布 report.md：訂單總數 150；異常總數與整體異常率因缺少欄位未提供。", [MISSING_REPORT]) is None
    assert completion_summary("已發布 report.md。", [MISSING_REPORT]) is None


MEETINGS = (
    "## 1. Q3 planning\n- **會議名稱**: Q3 planning\n- **決議**: ship the export feature first.\n- **行動負責人**: Priya\n\n"
    "## 2. Sprint retro\n- **會議名稱**: Sprint retro\n- **決議**: 未提供\n- **行動負責人**: Leo\n"
)


def test_label_with_a_value_elsewhere_does_not_reject_a_clause_about_that_value() -> None:
    # Unseen batch 01: the reply correctly stated planning.md's decision, but the
    # decision is missing only for retro.md, so the clause is not an overclaim.
    reply = "已發布 meetings.md：Q3 planning 的決議為 ship the export feature first，行動負責人 Priya。"
    assert completion_summary(reply, [MEETINGS]) is None


def test_label_missing_everywhere_is_still_checked() -> None:
    all_missing = MEETINGS.replace("- **決議**: ship the export feature first.", "- **決議**: 未提供")
    failure = completion_summary("已發布 meetings.md，列出兩份會議的決議與行動負責人。", [all_missing])
    assert failure is not None and "決議" in failure.message


def test_summary_numbers_must_appear_in_the_artifact() -> None:
    failure = completion_summary("已發布 report.md，訂單總數 240。", [MISSING_REPORT])
    assert failure is not None and "240" in failure.message
    assert completion_summary("已發布 report.md，訂單總數 150。", [MISSING_REPORT]) is None
    # Small counts describe the delivery (for example "2 份文件"), not a sourced value.
    assert completion_summary("已發布 2 份文件的索引。", [MISSING_REPORT]) is None


def test_summary_check_ignores_the_server_evidence_appendix() -> None:
    report = MISSING_REPORT + "\n\n## Verified tool evidence\n- [E2] metrics.csv:L2-L3; sum(orders) = 150; 未提供 only here\n"
    assert completion_summary("已發布 report.md，訂單總數 150。", [report]) is None


def test_finalization_sees_the_published_body_and_corrects_an_overclaiming_reply(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_text("week,orders\n7,60\n8,90\n", encoding="utf-8")
    overclaim = "已發布 report.md，包含訂單總數、異常總數及整體異常率。"
    grounded = "已發布 report.md：訂單總數 150；異常總數與整體異常率未提供。"
    peer = CompletionPeer([
        json_decision_reply("plan_update", {"steps": ["檢查來源並計算可用資料。"]}),
        json_decision_reply("table_inspect", {"path": "metrics.csv"}),
        json_decision_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
        json_decision_reply("artifact_write", {"name": "report.md", "content": MISSING_REPORT, "citations": ["E2"]}),
        json_decision_reply("task_finish", {"summary": overclaim}),
        json_decision_reply("task_finish", {"summary": grounded}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(
            client, source, task="請分析 metrics.csv，計算訂單總數、異常總數及整體異常率，保存為 report.md。",
            sources=["metrics.csv"],
        ))
        assert run["status"] == "completed", run["error"]
        assert run["result"] == grounded

    finalization = peer.requests[4]
    visible = "\n".join(str(message.get("content") or "") for message in finalization["messages"])
    assert "| 異常總數 | 未提供 |" in visible, "the model must see what it published"
    assert "## Verified tool evidence" not in visible
    rejected = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert rejected["tool"] == "task_finish"
    assert rejected["error"]["code"] == "unsupported_completion_claim"
    assert rejected["error"]["correctable_fields"] == ["summary"]
    observed = next(event["data"] for event in events if event["kind"] == "correction.observed")
    assert observed["tool"] == "task_finish"
    assert all(event["data"]["context_budget"]["upper_bound_tokens"] <= 8192
               for event in events if event["kind"] == "model.requested")


def test_receipt_completion_is_not_summary_checked(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_text("week,orders\n7,60\n8,90\n", encoding="utf-8")
    peer = CompletionPeer([
        json_decision_reply("plan_update", {"steps": ["檢查來源並計算可用資料。"]}),
        json_decision_reply("table_inspect", {"path": "metrics.csv"}),
        json_decision_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
        json_decision_reply("artifact_write", {"name": "report.md", "content": MISSING_REPORT, "citations": ["E2"]}),
    ])

    async def select_published(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == 4:
            body = json.loads(request.content)
            published = json.loads(body["messages"][-1]["content"])["tool_result"]["value"]
            peer.replies.append(json_decision_reply("task_finish", {"artifact_ids": [published["id"]]}))
        return await peer(request)

    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(select_published))) as client:
        run, events = finish(client, create_task(client, source, sources=["metrics.csv"],
                                                 completion_workflow="artifact_receipt"))
        assert run["status"] == "completed", run["error"]
        assert not any(event["kind"] == "correction.available" for event in events)
