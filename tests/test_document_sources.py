"""PDF and Word files in a workspace are sources: the model reads their extracted text.

Extraction is bounded (file size, pages, unpacked size, extracted text), and the evidence
hash is the hash of the file's bytes, so a changed document is caught like any changed
source. A document without extractable text, an encrypted PDF or a damaged file is refused
with a reason instead of being read as empty.
"""
import hashlib
import io
from pathlib import Path

import docx
import httpx
import pypdf
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo import sources
from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply


def _pdf(pages: list[str]) -> bytes:
    """A minimal PDF whose pages each show one line of Helvetica text."""
    count = len(pages)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{4 + 2 * i} 0 R" for i in range(count)).encode(), count),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for index, text in enumerate(pages):
        stream = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text.encode("ascii")
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                       b"/Contents %d 0 R >>" % (5 + 2 * index))
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    output, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    output += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    output += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return output


def _docx() -> bytes:
    document = docx.Document()
    document.add_heading("Quarterly notes", level=1)
    document.add_paragraph("Revenue was 42.")
    table = document.add_table(rows=2, cols=2)
    for row, values in zip(table.rows, (("Region", "Owner"), ("North", "Leo"))):
        for cell, value in zip(row.cells, values):
            cell.text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _blank_pdf(*, password: str | None = None) -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    if password:
        writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _workspace(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_bytes(content)
    return root


def _client(tmp_path: Path, replies: list[httpx.Response] | None = None) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer(replies or []))))


def test_pdf_and_word_files_are_listed_and_previewed_as_text(tmp_path: Path) -> None:
    report = _pdf(["Revenue was 42.", "Costs were 30."])
    root = _workspace(tmp_path, {"report.pdf": report, "notes.docx": _docx()})
    with _client(tmp_path) as client:
        workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
        listed = {item["path"] for item in client.get(f"/api/workspaces/{workspace['id']}/sources").json()["sources"]}
        pdf = client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": "report.pdf"}).json()
        word = client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": "notes.docx"}).json()

    assert listed == {"report.pdf", "notes.docx"}
    assert pdf["sha256"] == hashlib.sha256(report).hexdigest() and pdf["bytes"] == len(report)
    assert pdf["converted_from"] == "pdf" and pdf["pages"] == 2
    assert "Revenue was 42." in pdf["text"] and "Costs were 30." in pdf["text"]
    assert pdf["text"].index("[page 1]") < pdf["text"].index("Revenue") < pdf["text"].index("[page 2]")
    assert word["converted_from"] == "docx"
    assert word["text"].splitlines()[:2] == ["Quarterly notes", "Revenue was 42."]
    assert "Region | Owner" in word["text"] and "North | Leo" in word["text"]


def test_a_run_cites_a_pdf_source(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"report.pdf": _pdf(["Revenue was 42."])})
    replies = [
        tool_reply("plan_update", {"steps": ["Read report.pdf", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "report.pdf", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Revenue was 42. [report.pdf E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ]
    with _client(tmp_path, replies) as client:
        workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": "Report the revenue.", "sources": ["report.pdf"]}).json()
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    read = next(event["data"]["result"] for event in events
                if event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read")
    assert "Revenue was 42." in read["text"] and read["converted_from"] == "pdf"


@pytest.mark.parametrize(("name", "content", "code", "reason"), [
    ("scan.pdf", _blank_pdf(), "source_unavailable", "no extractable text"),
    ("locked.pdf", _blank_pdf(password="secret"), "source_unavailable", "encrypted"),
    ("broken.pdf", b"%PDF-1.4\nthis is not a real document", "source_unavailable", "cannot be read"),
    ("broken.docx", b"PK\x03\x04 not a zip", "source_unavailable", "cannot be read"),
])
def test_unreadable_documents_are_refused_with_a_reason(tmp_path: Path, name: str, content: bytes, code: str, reason: str) -> None:
    root = _workspace(tmp_path, {name: content})
    with pytest.raises(sources.RunFailure) as refused:
        sources.read_source(root.resolve(), name)
    assert refused.value.code == code and reason in refused.value.message


def test_document_limits_bound_pages_unpacked_size_and_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path, {"long.pdf": _pdf(["one", "two", "three"]), "notes.docx": _docx(),
                                 "wordy.pdf": _pdf(["x" * 300])}).resolve()
    monkeypatch.setattr(sources, "MAX_DOCUMENT_PAGES", 2)
    with pytest.raises(sources.RunFailure, match="pages") as pages:
        sources.read_source(root, "long.pdf")
    monkeypatch.setattr(sources, "MAX_DOCUMENT_UNPACKED_BYTES", 1024)
    with pytest.raises(sources.RunFailure, match="unpacked") as unpacked:
        sources.read_source(root, "notes.docx")
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 100)
    with pytest.raises(sources.RunFailure) as text:
        sources.read_source(root, "wordy.pdf")
    assert pages.value.code == unpacked.value.code == text.value.code == "source_too_large"


def test_documents_larger_than_the_file_limit_are_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _pdf(["Revenue was 42."])
    root = _workspace(tmp_path, {"report.pdf": report, "facts.md": b"Revenue was 42.\n"}).resolve()
    monkeypatch.setattr(sources, "MAX_DOCUMENT_BYTES", len(report) - 1)
    assert [item["path"] for item in sources.catalog(root)] == ["facts.md"]
