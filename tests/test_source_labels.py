from pathlib import Path
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.web import create_app
from test_workbench import ReportPeer, create_task, finish, tool_reply


def test_source_read_exposes_literal_labels_with_lines_without_merging_roles(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = "# Handoff\nOwner: Rina\nIncident escalation contact: Ken\nOwner: Jo\n備註： 保留原文  \nhttps://example.com\nUnlabelled prose\n"
    (source / "handoff.md").write_text(text, encoding="utf-8", newline="\n")
    decisions = [
        ("plan_update", {"steps": ["Read the selected source and save a cited snapshot"]}, "plan"),
        ("source_read", {"path": "handoff.md"}, "read"),
        ("artifact_write", {"name": "snapshot.md", "content": "Source: handoff.md [E1]", "citations": ["E1"]}, "publish"),
    ]
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["handoff.md"], task="Read and cite handoff.md."))
        assert run["status"] == "completed", run["error"]
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "read")
        assert read["text"] == text
        assert read["labelled_lines"] == [
            {"line": 2, "label": "Owner", "value": "Rina"},
            {"line": 3, "label": "Incident escalation contact", "value": "Ken"},
            {"line": 4, "label": "Owner", "value": "Jo"},
            {"line": 5, "label": "備註", "value": "保留原文  "},
        ]
        assert "owner" not in read and "missing_fields" not in read
        transported = json.loads(requests[2]["messages"][-1]["content"])
        assert transported == read
        assert "Source: handoff.md [E1]" in client.get(run["artifacts"][0]["download_url"]).text


@pytest.mark.parametrize("text", ["Name: value\n" * 17, "Label: " + "x" * 2049 + "\n"])
def test_oversized_label_view_is_omitted_without_truncating_raw_source(tmp_path: Path, text: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text(text, encoding="utf-8", newline="\n")
    peer = ReportPeer()
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "read")
        assert "labelled_lines" not in read
        assert read["text"] == text
        transported = json.loads(peer.requests[2]["messages"][-1]["content"])
        assert transported == read
        assert text in client.get(run["artifacts"][0]["download_url"]).text


def test_requested_labels_distinguish_absent_role_from_contact_even_when_view_is_omitted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = "# Handoff\n" + "Unrelated: hint\n" * 17 + "Incident escalation contact: Lee.\nIncident escalation contact: Jo.\n"
    (source / "handoff.md").write_text(text, encoding="utf-8", newline="\n")
    decisions = [
        ("plan_update", {"steps": ["Query exact source labels and publish a cited snapshot"]}, "plan"),
        ("source_read", {"path": "handoff.md", "labels": ["Owner", "Incident escalation contact"]}, "query"),
        ("artifact_write", {"name": "snapshot.md", "content": "Source: handoff.md [E1]", "citations": ["E1"]}, "publish"),
    ]
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["handoff.md"], task="Query the source's exact labels."))
        assert run["status"] == "completed", run["error"]
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "query")
        assert read["text"] == text and "labelled_lines" not in read
        assert read["label_queries"] == {"complete": True, "matches": {
            "Owner": [],
            "Incident escalation contact": [{"line": 19, "value": "Lee."}, {"line": 20, "value": "Jo."}],
        }}
        assert json.loads(requests[2]["messages"][-1]["content"]) == read
        tool = next(item["function"] for item in requests[1]["tools"] if item["function"]["name"] == "source_read")
        assert "labels" in tool["parameters"]["properties"]
        assert set(run["sources"][0]) == {"path", "sha256", "bytes"}


@pytest.mark.parametrize("text", ["Owner: value\n" * 17, "Owner: " + "x" * 2049 + "\n"])
def test_incomplete_label_query_never_reports_an_absent_label(tmp_path: Path, text: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text(text, encoding="utf-8", newline="\n")
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": ["Query exact labels"]}, "plan")
        if len(requests) == 2:
            return tool_reply("source_read", {"path": "facts.md", "labels": ["Owner", "Absent"]}, "query")
        return httpx.Response(200, json={"choices": [{"message": {"content": "No artifact published."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "failed" and run["error"]["code"] == "missing_artifact"
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "query")
        assert read["text"] == text
        assert read["label_queries"] == {"complete": False, "matches": None}
        assert json.loads(requests[2]["messages"][-1]["content"]) == read


def test_csv_only_catalog_keeps_column_tools_without_document_label_schema(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.csv").write_text("name,count\na,1\n", encoding="utf-8", newline="\n")
    decisions = [
        ("plan_update", {"steps": ["Read the source schema"]}, "plan"),
        ("source_read", {"path": "facts.csv"}, "read"),
        ("artifact_write", {"name": "schema.md", "content": "Source: facts.csv [E1]", "citations": ["E1"]}, "publish"),
    ]
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, _ = finish(client, create_task(client, source, sources=["facts.csv"], task="Describe the selected source."))
        assert run["status"] == "completed", run["error"]
        catalog = {tool["function"]["name"]: tool["function"] for tool in requests[1]["tools"]}
        assert {"table_inspect", "table_aggregate", "number_calculate"} <= catalog.keys()
        assert "labels" not in catalog["source_read"]["parameters"]["properties"]
        assert "labels" not in catalog["source_read"]["description"]
        assert "source_read.labels" not in requests[1]["messages"][0]["content"]


@pytest.mark.parametrize(("text", "labels", "expected"), [
    ("# Support\nOwner:Rina\n\u5099\u8a3b\uff1a\u660e\u6587\nhttps://example.com\n", ["Owner", "\u5099\u8a3b"],
     {"complete": True, "matches": {"Owner": [{"line": 2, "value": "Rina"}], "\u5099\u8a3b": [{"line": 3, "value": "\u660e\u6587"}]}}),
    ("Owner://Rina\n", ["Owner"], {"complete": False, "matches": None}),
])
def test_compact_colon_labels_and_uri_ambiguity_never_become_false_absence(tmp_path: Path, text: str, labels: list[str], expected: dict) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text(text, encoding="utf-8", newline="\n")
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": ["Query literal labels"]}, "plan")
        if len(requests) == 2:
            return tool_reply("source_read", {"path": "facts.md", "labels": labels}, "query")
        return httpx.Response(200, json={"choices": [{"message": {"content": "No artifact published."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "failed" and run["error"]["code"] == "missing_artifact"
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "query")
        assert read["text"] == text
        assert read["label_queries"] == expected
        assert all(item["label"] != "https" for item in read.get("labelled_lines", []))
        assert json.loads(requests[2]["messages"][-1]["content"]) == read


def test_document_model_declares_raw_discovery_without_an_absence_claim(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = "# Notes\nOwner: Rina\n"
    (source / "facts.md").write_text(text, encoding="utf-8", newline="\n")
    decisions = [
        ("plan_update", {"steps": ["Discover the source syntax"]}, "plan"),
        ("source_read", {"path": "facts.md", "labels": []}, "read"),
        ("artifact_write", {"name": "snapshot.md", "content": "Source: facts.md [E1]", "citations": ["E1"]}, "publish"),
    ]
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(decisions):
            return tool_reply(*decisions[index])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "read")
        assert read["text"] == text and "label_queries" not in read
        tool = next(tool["function"] for tool in requests[1]["tools"] if tool["function"]["name"] == "source_read")
        assert "labels" in tool["parameters"]["required"]
        assert json.loads(requests[2]["messages"][-1]["content"]) == read
