import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from scripted import planning
from agent_harness.web import create_app
from test_workbench import create_task, finish, tool_reply


class CorrectingPeer:
    def __init__(self) -> None:
        self.calls = 0
        self.saw_error = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        index = self.calls
        self.calls += 1
        if index == 0:
            return tool_reply("plan_update", {"steps": ["Compute total orders", "Publish report"]}, "plan")
        if index == 1:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "total"}, "bad")
        if index == 2:
            result = json.loads(messages[-1]["content"])
            assert messages[-1]["tool_call_id"] == "bad"
            assert result["error"]["code"] == "invalid_arguments"
            assert result["error"]["retryable"] is True
            assert result["error"]["remaining_corrections"] == 1
            assert "sum" in result["error"]["message"]
            self.saw_error = True
            return tool_reply("plan_update", {"steps": ["Use supported sum operation", "Publish report"]}, "replan")
        if index == 3:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}, "fixed")
        if index == 4:
            result = json.loads(messages[-1]["content"])
            return tool_reply("artifact_write", {"name": "total.md", "content": f"Orders: {result['value']}", "citations": [result["evidence_id"]]}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Report saved."}}]})


def test_model_receives_rejection_and_revises_plan_and_arguments_within_same_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n120\n150\n", encoding="utf-8")
    peer = CorrectingPeer()
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "completed", run["error"]
        assert peer.saw_error
        assert run["usage"] == {"model_calls": 6, "tool_calls": 5}
        plans = [event["data"]["steps"] for event in events if event["kind"] == "plan.updated"]
        assert plans == [["Compute total orders", "Publish report"], ["Use supported sum operation", "Publish report"]]
        dispatched = [event["data"]["call_id"] for event in events if event["kind"] == "tool.dispatched"]
        assert dispatched == ["plan", "replan", "fixed", "write"]
        corrections = [event for event in events if event["kind"] == "correction.available"]
        assert len(corrections) == 1 and corrections[0]["data"]["call_id"] == "bad"
        text = client.get(run["artifacts"][0]["download_url"]).text
        assert "Orders: 270" in text and "sum(orders) = 270" in text


def test_repeated_bad_arguments_exhaust_corrections_without_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n120\n", encoding="utf-8")
    calls = 0

    async def repeats(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "total"}, f"bad-{calls}")

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(repeats))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "failed" and run["error"]["code"] == "correction_limit_exceeded"
        assert run["usage"] == {"model_calls": 3, "tool_calls": 3}
        assert calls == 3
        assert sum(event["kind"] == "correction.available" for event in events) == 2
        assert not any(event["kind"] == "tool.dispatched" for event in events)
        assert events[-1]["kind"] == "run.failed"


def test_artifact_and_replan_do_not_hide_an_unresolved_tool_error(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("orders\n120\n", encoding="utf-8")
    calls = 0

    async def claims_success(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("artifact_write", {"name": "early.md", "content": "Partial report"}, "early")
        if calls == 2:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "total"}, "bad")
        if calls == 3:
            return tool_reply("plan_update", {"steps": ["Pretend the report is complete"]}, "plan")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Everything succeeded!"}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(claims_success)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error"
        assert client.get(run["artifacts"][0]["download_url"]).text == "Partial report"
        assert not any(event["kind"] == "correction.observed" for event in events)
        assert not any(event["kind"] == "run.completed" for event in events)


def test_controlled_read_failure_is_labelled_and_model_retries_after_feedback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    calls = 0

    async def retry_read(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        messages = json.loads(request.content)["messages"]
        calls += 1
        if calls <= 2:
            if calls == 2:
                error = json.loads(messages[-1]["content"])["error"]
                assert error["code"] == "source_unavailable" and error["retryable"] is True
                assert "controlled" in error["message"].lower()
            return tool_reply("source_read", {"path": "facts.md"}, f"read-{calls}")
        if calls == 3:
            result = json.loads(messages[-1]["content"])
            return tool_reply("artifact_write", {"name": "owner.md", "content": result["text"], "citations": [result["evidence_id"]]}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})

    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(planning(retry_read)), fault_once_source_read=True)) as client:
        assert client.get("/api/config").json()["validation"] == {"fault_once_source_read": True}
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "completed", run["error"]
        assert run["validation"] == {"fault_once_source_read": True}
        assert run["usage"] == {"model_calls": 5, "tool_calls": 4}
        faults = [event for event in events if event["kind"] == "tool.fault_injected"]
        assert len(faults) == 1 and faults[0]["data"]["call_id"] == "read-1"
        assert len([event for event in events if event["kind"] == "correction.available"]) == 1
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith("Owner: Mei\n")
    with TestClient(create_app(store)) as client:
        assert client.get(f"/api/runs/{run['id']}").json()["validation"] == {"fault_once_source_read": True}


@pytest.mark.parametrize("tool,arguments", [
    ("source_read", {"path": "notes.md"}),
    ("table_aggregate", {"path": "other.csv", "column": "orders", "operation": "sum"}),
])
def test_unrelated_source_cannot_clear_a_failed_calculation(tmp_path: Path, tool: str, arguments: dict) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders\n120\n")
    (source / "other.csv").write_bytes(b"orders\n999\n")
    (source / "notes.md").write_bytes(b"Unrelated notes.")
    calls = 0

    async def unrelated(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("artifact_write", {"name": "early.md", "content": "Partial"}, "early")
        if calls == 2:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "total"}, "bad")
        if calls == 3:
            return tool_reply(tool, arguments, "unrelated")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(unrelated)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv", "other.csv", "notes.md"]))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error"
        assert not any(event["kind"] == "correction.observed" for event in events)


def test_preplanned_success_in_same_model_response_is_not_a_correction(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Owner: Mei\n")
    calls = 0

    async def preplanned(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("artifact_write", {"name": "early.md", "content": "Partial"}, "early")
        if calls == 2:
            first = tool_reply("source_read", {"path": "facts.md"}, "first").json()["choices"][0]["message"]
            second = tool_reply("source_read", {"path": "facts.md"}, "second").json()["choices"][0]["message"]["tool_calls"][0]
            first["tool_calls"].append(second)
            return httpx.Response(200, json={"choices": [{"message": first}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(preplanned)), fault_once_source_read=True)) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error"
        assert any(event["kind"] == "tool.completed" and event["data"]["call_id"] == "second" for event in events)
        assert not any(event["kind"] == "correction.observed" for event in events)


@pytest.mark.parametrize("limits,code,expected_calls", [
    ({"max_steps": 1}, "step_limit_exceeded", 1),
    ({"max_tool_calls": 1}, "tool_limit_exceeded", 2),
])
def test_corrections_share_the_original_run_limits(tmp_path: Path, limits: dict, code: str, expected_calls: int) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders\n120\n")
    calls = 0

    async def bad(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "total"}, f"bad-{calls}")

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(bad))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"], limits=limits))
        assert run["status"] == "failed" and run["error"]["code"] == code
        assert calls == expected_calls
        assert run["usage"]["tool_calls"] == 1
        assert not any(event["kind"] == "tool.dispatched" for event in events)


def test_artifact_execution_failure_is_not_returned_to_model_for_retry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"A fact.")
    store = tmp_path / "store"
    calls = 0

    async def blocked_publication(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        (store / "artifacts").write_bytes(b"Directory unavailable")
        return tool_reply("artifact_write", {"name": "report.md", "content": "Attempted publication"}, "write")

    with TestClient(create_app(store, model_transport=httpx.MockTransport(planning(blocked_publication)))) as client:
        run, events = finish(client, create_task(client, source))
        assert run["status"] == "failed" and run["error"]["code"] == "execution_error"
        assert calls == 1 and not run["artifacts"]
        assert [event["data"]["tool"] for event in events if event["kind"] == "tool.dispatched"] == ["plan_update", "artifact_write"]
        assert not any(event["kind"] == "correction.available" for event in events)


def test_later_decision_can_use_a_different_view_of_the_same_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders\n120\n")
    calls = 0

    async def alternative(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("source_read", {"path": "facts.csv"}, "failed-read")
        if calls == 2:
            return tool_reply("table_inspect", {"path": "facts.csv"}, "inspect-instead")
        if calls == 3:
            result = json.loads(json.loads(request.content)["messages"][-1]["content"])
            return tool_reply("artifact_write", {"name": "columns.md", "content": "Columns: " + ", ".join(result["columns"]), "citations": [result["evidence_id"]]}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(alternative)), fault_once_source_read=True)) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "completed", run["error"]
        corrected = [event["data"] for event in events if event["kind"] == "correction.observed"]
        assert len(corrected) == 1
        assert corrected[0]["successful_call_id"] == "inspect-instead"
        assert corrected[0]["failed_decision_steps"] == [2] and corrected[0]["decision_step"] == 3


def test_fixing_one_csv_column_does_not_clear_another_failed_column(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders,exceptions\n120,12\n")
    calls = 0

    async def columns(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("artifact_write", {"name": "partial.md", "content": "Partial"}, "early")
        if calls == 2:
            first = tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "total"}, "orders-bad").json()["choices"][0]["message"]
            second = tool_reply("table_aggregate", {"path": "facts.csv", "column": "exceptions", "operation": "total"}, "exceptions-bad").json()["choices"][0]["message"]["tool_calls"][0]
            first["tool_calls"].append(second)
            return httpx.Response(200, json={"choices": [{"message": first}]})
        if calls == 3:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}, "orders-fixed")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(columns)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error"
        corrected = [event["data"] for event in events if event["kind"] == "correction.observed"]
        assert corrected[0]["failed_call_ids"] == ["orders-bad"]


def test_different_numeric_operands_do_not_clear_the_failed_percentage(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders,exceptions\n120,12\n")
    calls = 0

    async def operands(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("artifact_write", {"name": "partial.md", "content": "Partial"}, "early")
        if calls in {2, 3}:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders" if calls == 2 else "exceptions", "operation": "sum"}, f"aggregate-{calls}")
        if calls == 4:
            return tool_reply("number_calculate", {"operation": "percentage", "numerator": "E2", "denominator": "E1"}, "bad-rate")
        if calls == 5:
            return tool_reply("number_calculate", {"operation": "ratio_percent", "numerator": "E1", "denominator": "E2"}, "different-rate")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(operands)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error"
        assert not any(event["kind"] == "correction.observed" for event in events)


@pytest.mark.parametrize("bad_column", ["orderz", "month"])
def test_invalid_column_can_be_corrected_without_changing_the_calculation(tmp_path: Path, bad_column: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders,exceptions,month\n120,12,Jan\n")
    calls = 0

    async def fix_column(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": bad_column, "operation": "sum"}, "bad-column")
        if calls == 2:
            result = json.loads(json.loads(request.content)["messages"][-1]["content"])
            assert result["error"]["correctable_fields"] == ["column"]
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orders", "operation": "sum"}, "fixed-column")
        if calls == 3:
            result = json.loads(json.loads(request.content)["messages"][-1]["content"])
            return tool_reply("artifact_write", {"name": "total.md", "content": f"Orders: {result['value']}", "citations": [result["evidence_id"]]}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(fix_column)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "completed", run["error"]
        assert any(event["kind"] == "correction.observed" for event in events)
        assert "Orders: 120" in client.get(run["artifacts"][0]["download_url"]).text


def test_successive_parameter_fixes_resolve_the_original_error_chain(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_bytes(b"orders\n120\n")
    calls = 0

    async def chain(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return tool_reply("table_aggregate", {"path": "facts.csv", "column": "orderz" if calls < 3 else "orders",
                "operation": "total" if calls == 1 else "sum"}, f"attempt-{calls}")
        if calls == 4:
            result = json.loads(json.loads(request.content)["messages"][-1]["content"])
            return tool_reply("artifact_write", {"name": "total.md", "content": f"Orders: {result['value']}", "citations": [result["evidence_id"]]}, "write")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})

    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(chain)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "completed", run["error"]
        corrected = [event["data"] for event in events if event["kind"] == "correction.observed"]
        assert corrected[0]["failed_call_ids"] == ["attempt-1", "attempt-2"]
        assert corrected[0]["failed_decision_steps"] == [2, 3]
        assert corrected[0]["decision_step"] == 4
        assert sum(event["kind"] == "correction.available" for event in events) == 2


def test_raw_read_retry_can_omit_the_original_empty_label_intent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Owner: Rina\n", encoding="utf-8", newline="\n")
    decisions = [
        ("plan_update", {"steps": ["Read and publish source facts"]}, "plan"),
        ("source_read", {"path": "facts.md", "labels": []}, "empty-intent"),
        ("source_read", {"path": "facts.md"}, "raw-retry"),
        ("artifact_write", {"name": "snapshot.md", "content": "Source: facts.md [E1]", "citations": ["E1"]}, "publish"),
    ]
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer), fault_once_source_read=True)) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        error = next(event for event in events if event["kind"] == "correction.available")
        fixed = next(event for event in events if event["kind"] == "correction.observed")
        assert error["data"]["call_id"] == "empty-intent"
        assert fixed["data"]["successful_call_id"] == "raw-retry"
        assert any(event["kind"] == "model.requested" and error["sequence"] < event["sequence"] < fixed["sequence"] for event in events)
        original = next(event["data"] for event in events if event["kind"] == "tool.requested" and event["data"]["call_id"] == "empty-intent")
        assert original["arguments"] == {"path": "facts.md", "labels": []}


def test_table_inspection_cannot_resolve_an_explicit_label_query_failure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("Owner\nRina\n", encoding="utf-8", newline="\n")
    decisions = [
        ("plan_update", {"steps": ["Query exact labels"]}, "plan"),
        ("source_read", {"path": "facts.csv", "labels": ["Owner"]}, "query-failed"),
        ("table_inspect", {"path": "facts.csv"}, "different-view"),
        ("artifact_write", {"name": "schema.md", "content": "Inspected source: facts.csv [E1]", "citations": ["E1"]}, "publish"),
    ]
    calls = 0
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        index = calls
        calls += 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer), fault_once_source_read=True)) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.csv"]))
        assert run["status"] == "failed" and run["error"]["code"] == "unresolved_tool_error", run["error"]
        assert any(event["kind"] == "tool.completed" and event["data"]["call_id"] == "different-view" for event in events)
        assert not any(event["kind"] == "correction.observed" for event in events)
