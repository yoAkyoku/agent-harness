"""Public HTTP regression for citation-only correction on a pinned v1 report."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish


def test_revision_citation_correction_projects_pinned_text_and_publishes_v2(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "metrics.csv").write_text(
        "week,orders,exceptions\n1,120,12\n2,150,9\n3,180,9\n", encoding="utf-8", newline="",
    )
    base_text = (Path(__file__).parent / "fixtures/qwen35-pinned-revision-base.md").read_text(encoding="utf-8")
    base_body, marker, _ = base_text.partition("\n## Verified tool evidence")
    assert marker and len(base_text.encode("utf-8")) == 744
    base_body = base_body.rstrip()
    revised_content = base_body + "\n\n## 結論\n整體異常率為 6.67%，建議持續追蹤。\n"
    first_retry_content = revised_content + marker + base_text.partition(marker)[2]

    def call(name: str, arguments: dict | None = None) -> dict:
        return {"name": name, "arguments": arguments or {}}

    def decision(*calls: dict) -> str:
        return json.dumps({"calls": list(calls)}, ensure_ascii=False, separators=(",", ":"))

    replies = [
        decision(call("plan_update", {"steps": ["檢查輸入", "依證據計算", "保存報告"]})),
        decision(call("workspace_list")),
        decision(call("table_inspect", {"path": "metrics.csv"})),
        decision(
            call("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}),
            call("table_aggregate", {"path": "metrics.csv", "column": "exceptions", "operation": "sum"}),
        ),
        decision(call("number_calculate", {"operation": "ratio_percent", "numerator": "E3", "denominator": "E2"})),
        decision(call("artifact_write", {
            "name": "report.md", "content": base_body, "citations": ["E1", "E2", "E3", "E4"],
        })),
        decision(call("task_finish", {"summary": "已保存含計算結果與來源的 report.md。"})),
        decision(call("plan_update", {"steps": ["讀取 v1", "修正引用", "保存 v2"]})),
        decision(call("base_artifact_read")),
        decision(call("artifact_write", {
            "name": "report.md", "content": first_retry_content, "citations": ["E1", "E2", "E3", "E4"],
        })),
        decision(call("artifact_write", {
            "name": "report.md", "content": first_retry_content, "citations": ["E1"],
        })),
        decision(call("artifact_write", {"name": "report.md", "content": revised_content, "citations": ["E1"]})),
        decision(call("task_finish", {"summary": "已更新 report.md，保留既有數值與引用。"})),
    ]
    requests: list[dict] = []

    async def replay(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "contract-model", "digest": "a" * 64}]})
        assert request.url.path == "/v1/chat/completions"
        requests.append(json.loads(request.content))
        reply = replies[len(requests) - 1]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": reply,
        }}]})

    settings = ModelSettings.local(
        "contract-model", stream=False, decision_protocol="json_tools", server_controlled_initial_plan=True,
    )
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(replay))) as client:
        original, _ = finish(client, create_task(client, source, task=(
            "請分析 metrics.csv，計算訂單總數、異常總數及整體異常率（異常數 / 訂單數 × 100），"
            "保存為 report.md，附可追溯來源。若缺少必要欄位，明確指出缺少什麼；"
            "只計算有證據的數值，不能補零或猜測異常率。"
        ), sources=["metrics.csv"]))
        assert original["status"] == "completed", original["error"]
        original_artifact = original["artifacts"][0]
        original_bytes = client.get(original_artifact["download_url"]).content
        assert original_bytes == base_text.encode("utf-8")
        assert original_artifact["sha256"] == "28e1cc2a46e670d3cb3eb91fd7b3df977d25ae18a50b821b5d479b4666f0661b"

        response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
            "task": "請以 metrics.csv 為來源，在 report.md 末尾補上結論，保留 v1 的數值、計算與引用；以 v1 建立新版本。",
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
        assert client.get(original_artifact["download_url"]).content == original_bytes
        revised_text = client.get(revised["artifacts"][0]["download_url"]).text
        assert "整體異常率為 6.67%" in revised_text

    corrections = [event["data"] for event in events if event["kind"] == "correction.available"]
    assert len(corrections) == 2
    assert corrections[0]["error"]["code"] == "invalid_evidence"
    assert corrections[0]["error"]["correctable_fields"] == ["citations"]
    assert corrections[1]["error"]["code"] == "evidence_section_mismatch"
    assert corrections[1]["error"]["correctable_fields"] == ["content"]
    retry_request = requests[10]
    retry_budget = next(event["data"]["context_budget"] for event in events
                        if event["kind"] == "model.requested" and event["data"]["step"] == 4)
    assert retry_budget["upper_bound_tokens"] <= 8192
    saved_state = next(message for message in retry_request["messages"]
                       if message["role"] == "system" and message["content"].startswith("Saved executor state"))
    state_json = saved_state["content"].split("source_ref merges exact evidence_sources fields:\n", 1)[1]
    projected = json.loads(state_json)
    assert "text" not in projected["verified_evidence"]["E1"]
    assert any(event["kind"] == "context.pruned"
               and event["data"].get("reason") == "artifact_correction_pinned_text" for event in events)
    assert not any(event["kind"] == "context.limited" for event in events)
    assert len(requests) == len(replies)
