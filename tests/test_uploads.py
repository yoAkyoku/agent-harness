"""Uploads: the workbench saves a file into the workspace's `uploads/` folder, where it becomes a source.

An upload is checked before anything is written: a plain file name with a source type, within
that type's size limit, and readable the way a source will be read (UTF-8 text, or a PDF/Word
file with extractable text). It never overwrites: re-sending identical bytes returns the saved
file, and different bytes under an existing name are refused.
"""
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import sources
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_document_sources import _blank_pdf, _pdf


@pytest.fixture
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "facts.md").write_bytes(b"Revenue was 42.\n")
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(CompletionPeer([])))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
        yield client, workspace["id"], root


def _upload(client: TestClient, workspace: str, name: str, content: bytes) -> httpx.Response:
    return client.post(f"/api/workspaces/{workspace}/uploads", params={"name": name}, content=content,
                       headers={"Content-Type": "application/octet-stream"})


def test_an_upload_lands_in_uploads_and_becomes_a_source(setup) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup
    report = _pdf(["Revenue was 42."])
    notes = _upload(client, workspace, "meeting notes.md", "Owner: Leo\n".encode())
    pdf = _upload(client, workspace, "report.pdf", report)

    assert notes.status_code == 201, notes.text
    assert notes.json() == {"path": "uploads/meeting notes.md", "bytes": 11,
                            "sha256": hashlib.sha256(b"Owner: Leo\n").hexdigest(), "created": True}
    assert pdf.status_code == 201 and pdf.json()["path"] == "uploads/report.pdf"
    assert (root / "uploads" / "report.pdf").read_bytes() == report
    listed = {item["path"] for item in client.get(f"/api/workspaces/{workspace}/sources").json()["sources"]}
    assert listed == {"facts.md", "uploads/meeting notes.md", "uploads/report.pdf"}
    preview = client.get(f"/api/workspaces/{workspace}/source", params={"path": "uploads/report.pdf"}).json()
    assert "Revenue was 42." in preview["text"]


def test_uploads_never_overwrite(setup) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup
    assert _upload(client, workspace, "notes.md", b"first\n").status_code == 201
    again = _upload(client, workspace, "notes.md", b"first\n")
    changed = _upload(client, workspace, "notes.md", b"second\n")

    assert again.status_code == 200 and again.json()["created"] is False
    assert changed.status_code == 409
    assert (root / "uploads" / "notes.md").read_bytes() == b"first\n"


@pytest.mark.parametrize("name", ["../escape.md", "a/b.md", "a\\b.md", ".hidden.md", "tool.exe", "CON.md",
                                  "notes.md ", "x" * 121 + ".md", "bad:name.md", "tab\tname.md"])
def test_unsafe_or_unsupported_names_are_refused(setup, name: str) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup
    response = _upload(client, workspace, name, b"text\n")
    assert response.status_code == 422, (name, response.text)
    assert response.json()["detail"]["code"] == "invalid_upload_name"
    assert not (root / "uploads").exists()


@pytest.mark.parametrize(("name", "content", "code"), [
    ("latin.txt", b"caf\xe9\n", "source_unavailable"),
    ("scan.pdf", _blank_pdf(), "source_unavailable"),
    ("broken.docx", b"PK\x03\x04 not a zip", "source_unavailable"),
])
def test_unreadable_uploads_are_refused_before_anything_is_written(setup, name: str, content: bytes, code: str) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup
    response = _upload(client, workspace, name, content)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == code
    assert not (root / "uploads" / name).exists()
    assert not any((root / "uploads").glob("*")) if (root / "uploads").exists() else True


def test_oversized_uploads_are_refused(setup, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 10)
    response = _upload(client, workspace, "long.md", b"x" * 11)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "source_too_large"
    assert not (root / "uploads" / "long.md").exists()


def test_an_uploads_path_that_is_not_a_plain_folder_is_refused(setup) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup
    (root / "uploads").write_bytes(b"a file, not a folder")
    response = _upload(client, workspace, "notes.md", b"text\n")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "uploads_unavailable"


def test_unknown_workspace_is_404(setup) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = setup
    assert _upload(client, "0" * 32, "notes.md", b"text\n").status_code == 404


def test_a_workspace_that_cannot_be_written_is_refused_clearly(setup, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, workspace, root = setup

    def read_only(*args: object, **kwargs: object) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", read_only)  # as on a workspace mounted read-only (docker -v ...:ro)
    response = _upload(client, workspace, "notes.md", b"text\n")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "uploads_unavailable"
    assert "read-only" in response.json()["detail"]["message"]
