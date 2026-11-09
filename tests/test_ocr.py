"""OCR for scanned PDFs: optional, through a local Tesseract the user names with --tesseract.

Only a PDF with no text layer at all is sent to OCR. Each page is rendered to a grayscale image
(pypdfium2) and passed to the OCR program; the text is marked `[page N, OCR]` and the source
carries `ocr: "tesseract"`, so the model and the user can tell recognized text from a text layer.
Results are cached by the file's hash, and OCR is limited in pages and time. These tests use a
stand-in program in place of Tesseract; the real engine is not exercised here.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import sources
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_document_sources import _blank_pdf, _pdf
from test_workbench import finish, tool_reply

FAKE = str(Path(__file__).with_name("fake_tesseract.py"))


def _blank_pages(count: int) -> bytes:
    import io
    import pypdf
    writer = pypdf.PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _app(tmp_path: Path, mode: str, replies: list[httpx.Response] | None = None, languages: str = "eng") -> tuple[TestClient, Path]:
    counter = tmp_path / "ocr-calls.txt"
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(CompletionPeer(replies or [])),
                     ocr_command=[sys.executable, FAKE, mode, str(counter)], ocr_languages=languages)
    return TestClient(app), counter


def _workspace(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_bytes(content)
    return root


def _preview(client: TestClient, root: Path, name: str) -> httpx.Response:
    workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
    return client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": name})


def test_a_scanned_pdf_is_read_through_ocr(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"scan.pdf": _blank_pdf()})
    app, counter = _app(tmp_path, "text", languages="eng+chi_tra")
    with app as client:
        preview = _preview(client, root, "scan.pdf").json()

    assert preview["ocr"] == "tesseract" and preview["converted_from"] == "pdf" and preview["pages"] == 1
    assert preview["text"] == "[page 1, OCR]\nScanned revenue was 42. (eng+chi_tra)"
    assert counter.read_text(encoding="utf-8").splitlines() == ["eng+chi_tra"]


def test_a_run_cites_ocr_text_and_ocr_runs_once_per_document(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"scan.pdf": _blank_pdf()})
    replies = [
        tool_reply("plan_update", {"steps": ["Read the scan", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "scan.pdf", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Scanned revenue was 42. [scan.pdf E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ]
    app, counter = _app(tmp_path, "text", replies)
    with app as client:
        workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": "Report the revenue.", "sources": ["scan.pdf"]}).json()
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read")
    assert read["ocr"] == "tesseract" and "[page 1, OCR]" in read["text"]
    assert len(counter.read_text(encoding="utf-8").splitlines()) == 1  # admission, excerpts and the read share one OCR pass


def test_a_pdf_with_a_text_layer_is_not_sent_to_ocr(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"report.pdf": _pdf(["Revenue was 42."])})
    app, counter = _app(tmp_path, "text")
    with app as client:
        preview = _preview(client, root, "report.pdf").json()
    assert "ocr" not in preview and not counter.exists()


@pytest.mark.parametrize(("mode", "reason"), [("fail", "OCR failed"), ("empty", "OCR found no text")])
def test_failed_or_empty_ocr_is_refused_with_a_reason(tmp_path: Path, mode: str, reason: str) -> None:
    root = _workspace(tmp_path, {"scan.pdf": _blank_pdf()})
    app, _ = _app(tmp_path, mode)
    with app as client:
        response = _preview(client, root, "scan.pdf")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "source_unavailable" and reason in response.json()["detail"]["message"]


def test_ocr_is_limited_in_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "MAX_OCR_PAGES", 1)
    root = _workspace(tmp_path, {"long.pdf": _blank_pages(2)})
    app, counter = _app(tmp_path, "text")
    with app as client:
        response = _preview(client, root, "long.pdf")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "source_too_large"
    assert not counter.exists()


def test_without_ocr_a_scanned_pdf_is_still_refused(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"scan.pdf": _blank_pdf()})
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(CompletionPeer([])))) as client:
        response = _preview(client, root, "scan.pdf")
    assert response.status_code == 422 and "--tesseract" in response.json()["detail"]["message"]


@pytest.mark.parametrize(("arguments", "message"), [
    (["--tesseract", "no-such-folder/tesseract.exe"], "--tesseract"),
    (["--tesseract", sys.executable, "--ocr-languages", "eng;rm"], "--ocr-languages"),
    (["--ocr-languages", "eng"], "--ocr-languages"),
])
def test_cli_validates_ocr_options(tmp_path: Path, arguments: list[str], message: str) -> None:
    result = subprocess.run([sys.executable, "-m", "agent_harness", "--store", str(tmp_path / "store"), *arguments],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and message in result.stderr


TESSERACT = shutil.which("tesseract")


@pytest.mark.skipif(TESSERACT is None and not os.environ.get("REQUIRE_TESSERACT"),
                    reason="Tesseract is not installed here; CI installs it on Ubuntu and requires this test.")
def test_real_tesseract_reads_a_rendered_page() -> None:
    """The real engine, end to end from a PDF page: render at 300 dpi, write PGM, run Tesseract, read stdout."""
    assert TESSERACT, "REQUIRE_TESSERACT is set, but tesseract is not on PATH"
    sources.configure_ocr([TESSERACT], "eng")
    try:
        text = sources._ocr_text(_pdf(["Revenue was 42 million in Q3."]), 1)
    finally:
        sources.configure_ocr(None)
    assert text.startswith("[page 1, OCR]\n")
    assert "Revenue was 42 million" in text
