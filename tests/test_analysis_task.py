import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import pytest

from agent_harness_demo.web import create_app
from test_workbench import create_task, finish, tool_reply


class AnalysisPeer:
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        steps = [
            ("plan_update", {"steps": ["Inspect selected table", "Compute exception percentage", "Publish findings"]}),
            ("table_inspect", {"path": "facts.csv"}),
            ("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}),
            ("table_aggregate", {"path": "facts.csv", "column": "exceptions", "operation": "sum"}),
        ]
        index = len(results)
        if index < len(steps):
            name, arguments = steps[index]
        elif index == 4:
            name, arguments = "number_calculate", {"operation": "ratio_percent", "numerator": results[3]["evidence_id"], "denominator": results[2]["evidence_id"]}
        elif index == 5:
            name, arguments = "artifact_write", {"name": "analysis.md", "content": f"Exception percentage: {results[4]['value']}%", "citations": [results[4]["evidence_id"]]}
        else:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Analysis saved."}}]})
        return tool_reply(name, arguments, f"step-{index}")


def test_analysis_plan_numbers_and_transitive_source_citations_are_persisted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("month,orders,exceptions\nJan,120,12\nFeb,150,9\nMar,180,9\n", encoding="utf-8")
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(AnalysisPeer()))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "completed", run["error"]
        plans = [event["data"] for event in events if event["kind"] == "plan.updated"]
        assert plans == [{"steps": ["Inspect selected table", "Compute exception percentage", "Publish findings"]}]
        results = [event["data"]["result"] for event in events if event["kind"] == "tool.completed"]
        assert results[1]["columns"] == ["month", "orders", "exceptions"]
        assert results[1]["rows"] == 3
        assert results[2]["value"] == "450"
        assert results[3]["value"] == "30"
        assert results[4]["value"] == "6.666667"
        artifact = run["artifacts"][0]
        text = client.get(artifact["download_url"]).text
        assert "Exception percentage: 6.666667%" in text
        assert "facts.csv:L2-L4" in text
        assert run["sources"][0]["sha256"] in text
        assert "ratio_percent" in text
    with TestClient(create_app(store)) as client:
        assert client.get(artifact["download_url"]).text == text
        assert client.get(f"/api/runs/{run['id']}/events").json()["events"] == events


def test_body_evidence_ids_are_reconciled_with_declared_citations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n60\n90\n", encoding="utf-8")
    decisions = [
        ("plan_update", {"steps": ["檢查資料", "計算訂單數", "保存報告"]}, "plan"),
        ("table_inspect", {"path": "facts.csv"}, "inspect"),
        ("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}, "sum"),
        ("artifact_write", {
            "name": "report.md",
            "content": "# 訂單總數\n\n訂單總數：150 [facts.csv E2]\n來源：facts.csv [facts.csv E1]",
            "citations": ["E2"],
        }, "write"),
    ]

    async def peer(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        results = [item for item in messages if item["role"] == "tool"]
        if len(results) < len(decisions):
            return tool_reply(*decisions[len(results)])
        return httpx.Response(200, json={"choices": [{"message": {"content": "報告已完成。"}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(
            client, source, sources=["facts.csv"], task="請計算總訂單數並保存繁體中文報告。",
        ))
        assert run["status"] == "completed", (run["error"], events)
        assert not [event for event in events if event["kind"] == "correction.available"]
        body = client.get(run["artifacts"][0]["download_url"]).text

    assert "150 [facts.csv E2]" in body
    assert "[E1] facts.csv:L2-L3" in body
    assert "[E2] facts.csv:L2-L3" in body


@pytest.mark.parametrize("name,arguments,code", [
    ("table_aggregate", {"path": "other.csv", "column": "orders", "operation": "sum"}, "source_forbidden"),
    ("number_calculate", {"operation": "ratio_percent", "numerator": "E1", "denominator": "E2"}, "invalid_evidence"),
    ("artifact_write", {"name": "forged.md", "content": "Unsupported claim", "citations": ["E99"]}, "invalid_evidence"),
])
def test_analysis_tools_cannot_read_unselected_sources_or_invent_evidence(tmp_path: Path, name: str, arguments: dict, code: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n1\n", encoding="utf-8")
    (source / "other.csv").write_text("orders\n999\n", encoding="utf-8")

    async def invalid(request: httpx.Request) -> httpx.Response:
        return tool_reply(name, arguments)

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(invalid))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], limits={"max_corrections": 0}))
        assert run["status"] == "failed"
        assert run["error"]["code"] == code
        assert not run["artifacts"]
        assert not any(event["kind"] == "tool.dispatched" for event in events)


def test_csv_arithmetic_requires_numeric_evidence_before_publication_and_can_be_corrected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n60\n90\n", encoding="utf-8")
    decisions = [
        ("plan_update", {"steps": ["Read orders", "Compute total", "Publish report"]}, "plan"),
        ("source_read", {"path": "facts.csv"}, "read"),
        ("artifact_write", {"name": "report.md", "content": "Total orders: 60 + 90 = 150 [E1]", "citations": ["E1"]}, "uncomputed"),
        ("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}, "compute"),
        ("artifact_write", {"name": "report.md", "content": "Total orders: 60 + 90 = 150 [E2]", "citations": ["E2"]}, "corrected"),
    ]
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Report saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], task="Compute total orders and publish report.md."))
        assert run["status"] == "completed", run["error"]
        errors = [event for event in events if event["kind"] == "correction.available"]
        assert len(errors) == 1 and errors[0]["data"]["call_id"] == "uncomputed"
        assert errors[0]["data"]["error"]["code"] == "invalid_evidence"
        assert set(errors[0]["data"]["error"]["correctable_fields"]) == {"content", "citations"}
        assert "uncomputed" not in [event["data"]["call_id"] for event in events if event["kind"] == "tool.dispatched"]
        computed = next(event for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "compute")
        assert computed["data"]["result"]["value"] == "150"
        assert any(event["kind"] == "model.requested" and errors[0]["sequence"] < event["sequence"] < computed["sequence"] for event in events)
        assert any(event["kind"] == "correction.observed" and event["data"]["successful_call_id"] == "corrected" for event in events)
        assert len(run["artifacts"]) == 1
        body = client.get(run["artifacts"][0]["download_url"]).text
        assert "60 + 90 = 150" in body and "sum(orders) = 150" in body


def test_csv_source_formula_can_be_quoted_without_an_unrequested_calculation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("formula\n60 + 90 = 150\n", encoding="utf-8")
    decisions = [
        ("plan_update", {"steps": ["Inspect and quote the formula"]}, "plan"),
        ("table_inspect", {"path": "facts.csv"}, "inspect"),
        ("artifact_write", {"name": "quote.md", "content": "Source formula: 60+90=150 [E1]", "citations": ["E1"]}, "quote"),
    ]
    count = 0
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= len(decisions):
            return tool_reply(*decisions[count - 1])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Quoted."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], task="Quote the formula already present in the selected CSV."))
        assert run["status"] == "completed", run["error"]
        assert not any(event["kind"] == "correction.available" for event in events)
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] in {"table_aggregate", "number_calculate"} for event in events)
        assert "Source formula: 60+90=150" in client.get(run["artifacts"][0]["download_url"]).text


def test_computed_csv_results_allow_thousands_formatting_and_sentence_punctuation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n1000\n500\n", encoding="utf-8")
    decisions = [
        ("plan_update", {"steps": ["Compute and publish total"]}, "plan"),
        ("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}, "sum"),
        ("artifact_write", {"name": "report.md", "content": "Total: 1,000 + 500 = 1,500. [E1]", "citations": ["E1"]}, "publish"),
    ]
    count = 0
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= len(decisions):
            return tool_reply(*decisions[count - 1])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "completed", run["error"]
        assert not any(event["kind"] == "correction.available" for event in events)
        assert "1,000 + 500 = 1,500." in client.get(run["artifacts"][0]["download_url"]).text


def test_formula_in_csv_cell_can_be_quoted_from_source_read(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("row,formula\n1,600+900=1500\n", encoding="utf-8")
    decisions = [
        ("plan_update", {"steps": ["Inspect and quote the formula"]}, "plan"),
        ("source_read", {"path": "facts.csv"}, "read"),
        ("artifact_write", {"name": "quote.md", "content": "Source formula: 600+900=1500 [E1]", "citations": ["E1"]}, "quote"),
    ]
    count = 0
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= len(decisions):
            return tool_reply(*decisions[count - 1])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Quoted."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], task="Quote the formula already present in the selected CSV."))
        assert run["status"] == "completed", run["error"]
        assert not any(event["kind"] == "correction.available" for event in events)
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] in {"table_aggregate", "number_calculate"} for event in events)
        assert "Source formula: 600+900=1500" in client.get(run["artifacts"][0]["download_url"]).text


def test_arithmetic_shaped_csv_filename_remains_a_valid_source_reference(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "600+900=1500.csv").write_text("orders\n60\n90\n", encoding="utf-8")
    decisions = [
        ("plan_update", {"steps": ["Read and cite the source filename"]}, "plan"),
        ("source_read", {"path": "600+900=1500.csv"}, "inspect"),
        ("artifact_write", {"name": "quote.md", "content": "Source: `600+900=1500.csv` [E1]", "citations": ["E1"]}, "quote"),
    ]
    count = 0
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= len(decisions):
            return tool_reply(*decisions[count - 1])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Quoted."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["600+900=1500.csv"], task="Identify the selected CSV by its original filename."))
        assert run["status"] == "completed", run["error"]
        assert not any(event["kind"] == "correction.available" for event in events)
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] in {"table_aggregate", "number_calculate"} for event in events)
        assert "Source: `600+900=1500.csv`" in client.get(run["artifacts"][0]["download_url"]).text
