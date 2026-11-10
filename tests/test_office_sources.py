"""Excel, PowerPoint and image sources, and tables too large to read whole.

- An .xlsx file is a source whose sheets are tables: `table_inspect` and `table_aggregate` take an
  optional `sheet` (the first sheet by default) and compute over the whole sheet on the server.
- A large table (CSV or sheet) is never read into the model's context whole: `source_read` returns
  a labelled preview (headers, first rows, total rows, `preview: true`), and the table tools compute
  over every row. Nothing is cut silently.
- A .pptx file is read as text per slide; a .png/.jpg image goes through OCR when --tesseract is set.
"""
import io
from pathlib import Path
import sys

import httpx
import openpyxl
import pptx
import pytest
from fastapi.testclient import TestClient

from agent_harness import sources
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_ocr import FAKE
from test_workbench import finish, tool_reply


def _xlsx() -> bytes:
    book = openpyxl.Workbook()
    sales = book.active
    sales.title = "Sales"
    sales.append(["Region", "Revenue"])
    for region, revenue in (("North", 120), ("South", 80.5), ("East", 42)):
        sales.append([region, revenue])
    notes = book.create_sheet("Notes")
    notes.append(["Owner", "Topic"])
    notes.append(["Leo", "Q3 review"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _pptx() -> bytes:
    deck = pptx.Presentation()
    for title, body in (("Q3 review", "Revenue was 242.5"), ("Next steps", "Leo owns the follow-up")):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


def _workspace(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_bytes(content)
    return root


def _client(tmp_path: Path, replies: list[httpx.Response] | None = None, **options: object) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer(replies or [])), **options))  # type: ignore[arg-type]


def _preview(client: TestClient, root: Path, name: str) -> httpx.Response:
    workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
    return client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": name})


def _run(client: TestClient, root: Path, task: str, names: list[str]) -> tuple[dict, list[dict]]:
    workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": task, "sources": names}).json()
    return finish(client, run)


def test_a_workbook_is_read_sheet_by_sheet(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"q3.xlsx": _xlsx()})
    with _client(tmp_path) as client:
        preview = _preview(client, root, "q3.xlsx").json()
    assert preview["converted_from"] == "xlsx" and preview["sheets"] == ["Sales", "Notes"]
    assert "[sheet Sales]\nRegion,Revenue\nNorth,120\nSouth,80.5\nEast,42" in preview["text"]
    assert "[sheet Notes]\nOwner,Topic\nLeo,Q3 review" in preview["text"]


def test_table_tools_compute_over_a_chosen_sheet(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"q3.xlsx": _xlsx()})
    replies = [
        tool_reply("plan_update", {"steps": ["Inspect the sales sheet", "Total the revenue", "Publish"]}, "plan"),
        tool_reply("table_inspect", {"path": "q3.xlsx", "sheet": "Sales"}, "inspect"),
        tool_reply("table_aggregate", {"path": "q3.xlsx", "sheet": "Sales", "column": "Revenue", "operation": "sum"}, "sum"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Total revenue: 242.5 [q3.xlsx E2]", "citations": ["E2"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: total revenue 242.5."}, "finish"),
    ]
    with _client(tmp_path, replies) as client:
        final, events = _run(client, root, "Total the Q3 revenue.", ["q3.xlsx"])

    assert final["status"] == "completed", final["error"]
    results = {event["data"]["tool"]: event["data"]["result"] for event in events if event["kind"] == "tool.completed"}
    assert results["table_inspect"]["sheet"] == "Sales" and results["table_inspect"]["columns"] == ["Region", "Revenue"]
    assert results["table_aggregate"]["value"] == "242.5" and results["table_aggregate"]["sheet"] == "Sales"


def test_sheet_mistakes_are_correctable(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"q3.xlsx": _xlsx(), "facts.csv": b"Region,Revenue\nNorth,1\n"})
    replies = [
        tool_reply("plan_update", {"steps": ["Inspect", "Publish"]}, "plan"),
        tool_reply("table_inspect", {"path": "q3.xlsx", "sheet": "Costs"}, "missing-sheet"),
        tool_reply("table_inspect", {"path": "facts.csv", "sheet": "Sales"}, "csv-sheet"),
    ]
    with _client(tmp_path, replies) as client:
        _, events = _run(client, root, "Inspect the tables.", ["q3.xlsx", "facts.csv"])
    errors = [event["data"]["error"] for event in events if event["kind"] == "correction.available"]
    assert errors[0]["code"] == "invalid_table" and "Sales" in errors[0]["message"] and "Notes" in errors[0]["message"]
    assert errors[1]["code"] == "invalid_arguments" and "sheet" in errors[1]["message"]


def test_a_large_table_is_previewed_for_the_model_and_computed_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 2048)  # a small budget stands in for a large file
    rows = "".join(f"r{index},{index}\n" for index in range(1, 1001))
    root = _workspace(tmp_path, {"big.csv": ("Item,Value\n" + rows).encode()})
    replies = [
        tool_reply("plan_update", {"steps": ["Read", "Total", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "big.csv", "labels": []}, "read"),
        tool_reply("table_aggregate", {"path": "big.csv", "column": "Value", "operation": "sum"}, "sum"),
        tool_reply("artifact_write", {"name": "total.md", "content": "Total value: 500500 [big.csv E2]", "citations": ["E2"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved total.md: total value 500500."}, "finish"),
    ]
    with _client(tmp_path, replies) as client:
        final, events = _run(client, root, "Total the values.", ["big.csv"])

    assert final["status"] == "completed", final["error"]
    results = {event["data"]["tool"]: event["data"]["result"] for event in events if event["kind"] == "tool.completed"}
    read = results["source_read"]
    assert read["preview"] is True and read["rows"] == 1000
    assert read["text"].startswith("[preview: 20 of 1000 rows; compute with table_inspect and table_aggregate]\nItem,Value\nr1,1\n")
    assert "r21," not in read["text"]
    assert results["table_aggregate"]["value"] == "500500"


def test_a_presentation_is_read_slide_by_slide(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"deck.pptx": _pptx()})
    with _client(tmp_path) as client:
        preview = _preview(client, root, "deck.pptx").json()
    assert preview["converted_from"] == "pptx" and preview["slides"] == 2
    assert preview["text"] == "[slide 1]\nQ3 review\nRevenue was 242.5\n\n[slide 2]\nNext steps\nLeo owns the follow-up"


def test_images_are_read_through_ocr_when_configured(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64  # the stand-in OCR program reads whatever image it is given
    root = _workspace(tmp_path, {"scan.png": png})
    counter = tmp_path / "calls.txt"
    with _client(tmp_path, ocr_command=[sys.executable, FAKE, "text", str(counter)]) as client:
        preview = _preview(client, root, "scan.png").json()
    with _client(tmp_path / "off") as client:
        refused = _preview(client, root, "scan.png")

    assert preview["converted_from"] == "png" and preview["ocr"] == "tesseract"
    assert preview["text"] == "[image, OCR]\nScanned revenue was 42. (eng)"
    assert refused.status_code == 422 and "--tesseract" in refused.json()["detail"]["message"]
