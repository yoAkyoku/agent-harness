import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import create_task, finish, tool_reply


def multi_tool_reply(*calls: tuple[str, dict, str]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": identity, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }}
            for name, arguments, identity in calls
        ],
    }}]})


def json_tool_reply(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": json.dumps({"calls": [{"name": name, "arguments": arguments}]}),
    }}]})


def multi_json_tool_reply(*calls: tuple[str, dict]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": json.dumps({"calls": [
            {"name": name, "arguments": arguments} for name, arguments in calls
        ]}),
    }}]})


def test_empty_plan_update_after_saved_plan_preserves_correction_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.csv").write_text("week,orders\n1,42\n", encoding="utf-8")
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["檢查來源", "計算並引用", "保存報告"]}, "plan"),
        tool_reply("workspace_list", {}, "catalog"),
        tool_reply("table_inspect", {"path": "metrics.csv"}, "inspect"),
        tool_reply("table_aggregate", {"path": "metrics.csv", "column": "orders", "operation": "sum"}, "orders-sum"),
        multi_tool_reply(
            ("artifact_write", {
                "name": "report.md", "content": "訂單總數：42 [metrics.csv E1]", "citations": ["E1"],
            }, "uncited-write"),
            ("plan_update", {"steps": []}, "empty-plan"),
        ),
        tool_reply("artifact_write", {
            "name": "report.md", "content": "訂單總數：42 [metrics.csv E2]", "citations": ["E2"],
        }, "corrected-write"),
        tool_reply("task_finish", {"summary": "Published report.md."}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, server_controlled_initial_plan=False)

    with TestClient(create_app(
        tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer)
    )) as client:
        run, events = finish(client, create_task(
            client, source, task="請計算 orders 總數並保存 report.md。", sources=["metrics.csv"]
        ))
        assert run["status"] == "completed", run["error"]
        artifact = client.get(run["artifacts"][-1]["download_url"]).text

    assert artifact.startswith("訂單總數：42 [metrics.csv E2]\n\n## Verified tool evidence")
    corrections = [event["data"] for event in events if event["kind"] == "correction.available"]
    assert [correction["error"]["code"] for correction in corrections] == ["missing_calculation_citation"]
    empty_update = next(
        event["data"]["result"] for event in events
        if event["kind"] == "tool.completed" and event["data"]["tool"] == "plan_update"
        and event["data"]["result"].get("no_op")
    )
    assert empty_update["steps"] == ["檢查來源", "計算並引用", "保存報告"]


def test_empty_plan_update_without_artifact_correction_is_still_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Revenue was 42.\n", encoding="utf-8")
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["檢查來源", "保存報告"]}, "plan"),
        tool_reply("workspace_list", {}, "catalog"),
        tool_reply("source_read", {"path": "facts.md"}, "source"),
        tool_reply("artifact_write", {
            "name": "report.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"],
        }, "write"),
        tool_reply("plan_update", {"steps": []}, "empty-plan"),
        tool_reply("task_finish", {"summary": "Published report.md."}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, server_controlled_initial_plan=False)

    with TestClient(create_app(
        tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer)
    )) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))

    assert run["status"] == "failed"
    assert any(event["kind"] == "tool.rejected" and event["data"]["tool"] == "plan_update"
               for event in events)
    assert not any(event["kind"] == "tool.completed" and event["data"]["tool"] == "plan_update"
                   and event["data"]["result"].get("no_op") for event in events)


def test_empty_plan_update_in_same_decision_as_publication_is_a_noop(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Revenue was 42.\n", encoding="utf-8")
    peer = CompletionPeer([
        json_tool_reply("plan_update", {"steps": ["檢查來源", "整理並保存報告"]}),
        json_tool_reply("workspace_list", {}),
        json_tool_reply("source_read", {"path": "facts.md"}),
        multi_json_tool_reply(
            ("artifact_write", {
                "name": "report.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"],
            }),
            ("plan_update", {"steps": []}),
        ),
        httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps({"reply": "已發布 report.md。"}),
        }}]}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools",
                                   server_controlled_initial_plan=False)

    with TestClient(create_app(
        tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer)
    )) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        assert len(run["artifacts"]) == 1

    empty_update = next(
        event["data"]["result"] for event in events
        if event["kind"] == "tool.completed" and event["data"]["tool"] == "plan_update"
        and event["data"]["result"].get("no_op")
    )
    assert empty_update["steps"] == ["檢查來源", "整理並保存報告"]
    assert not any(event["kind"] == "correction.available" and event["data"]["tool"] == "plan_update"
                   for event in events)
