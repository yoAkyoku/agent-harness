"""More source types: YAML and log files as text, and HTML pages, e-mails and notebooks as extracted text.

Like PDF and Word files, a converted source keeps the SHA-256 and size of the file's bytes, says what it
was converted from, and is refused with a reason when nothing readable comes out. HTML is reduced to
its visible text (no scripts or styles), an e-mail to its headers and body with attachments only named,
and a notebook to its cells and text outputs.
"""
import hashlib
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from agent_harness import sources
from agent_harness.contracts import RunFailure


def _write(tmp_path: Path, name: str, data: bytes) -> dict:
    (tmp_path / name).write_bytes(data)
    assert name in [item["path"] for item in sources.catalog(tmp_path)]
    result = sources.read_source(tmp_path, name)
    assert result["sha256"] == hashlib.sha256(data).hexdigest() and result["bytes"] == len(data)
    return result


def test_yaml_and_log_files_are_read_as_text(tmp_path: Path) -> None:
    yaml = _write(tmp_path, "deploy.yaml", "service: checkout\nreplicas: 3\n".encode())
    assert yaml["text"] == "service: checkout\nreplicas: 3\n" and "converted_from" not in yaml
    assert _write(tmp_path, "compose.yml", b"version: 3\n")["text"] == "version: 3\n"
    assert _write(tmp_path, "app.log", "10:00 started\n10:05 結帳中斷\n".encode())["text"].endswith("結帳中斷\n")


def test_an_html_file_becomes_its_visible_text(tmp_path: Path) -> None:
    page = ("<html><head><title>Refund  policy</title><style>p{color:red}</style><script>alert('x')</script></head>"
            "<body><h1>Refunds</h1><p>Within <b>14</b> days.</p><ul><li>Approver: 張雅婷</li></ul></body></html>")
    result = _write(tmp_path, "policy.html", page.encode())
    assert result["converted_from"] == "html" and result["title"] == "Refund policy"
    assert result["text"].startswith("[title] Refund policy\n")
    assert "Within 14 days." in result["text"] and "Approver: 張雅婷" in result["text"]
    assert "alert" not in result["text"] and "color:red" not in result["text"]
    with pytest.raises(RunFailure, match="no readable text"):
        _write(tmp_path, "empty.htm", b"<html><body><script>1</script></body></html>")


def test_an_email_becomes_its_headers_and_body_with_attachments_only_named(tmp_path: Path) -> None:
    message = EmailMessage()
    message["From"] = "Mei <mei@example.com>"
    message["To"] = "ops@example.com"
    message["Subject"] = "Checkout outage"
    message["Date"] = "Mon, 16 Nov 2026 09:00:00 +0800"
    message.set_content("Checkout was down for 47 minutes.\nHandled by Tsai.\n")
    message.add_attachment(b"%PDF-1.4 secret", maintype="application", subtype="pdf", filename="report.pdf")
    result = _write(tmp_path, "outage.eml", message.as_bytes())
    assert result["converted_from"] == "eml" and result["attachments"] == ["report.pdf"]
    assert result["text"] == ("[from] Mei <mei@example.com>\n[to] ops@example.com\n[date] Mon, 16 Nov 2026 09:00:00 +0800\n"
                              "[subject] Checkout outage\n\nCheckout was down for 47 minutes.\nHandled by Tsai.\n\n"
                              "[attachments, not read] report.pdf")
    html_only = EmailMessage()
    html_only["Subject"] = "Hello"
    html_only.set_content("<p>Budget is <b>7</b>.</p>", subtype="html")
    assert "Budget is 7." in _write(tmp_path, "html.eml", html_only.as_bytes())["text"]


def test_a_notebook_becomes_its_cells_and_text_outputs(tmp_path: Path) -> None:
    notebook = {"nbformat": 4, "cells": [
        {"cell_type": "markdown", "source": ["# Revenue\n", "Q3 figures."]},
        {"cell_type": "code", "source": "print(40 + 2)", "outputs": [
            {"output_type": "stream", "name": "stdout", "text": ["42\n"]},
            {"output_type": "display_data", "data": {"image/png": "iVBORw0KGgo=", "text/plain": ["<Figure>"]}}]},
    ]}
    result = _write(tmp_path, "analysis.ipynb", json.dumps(notebook).encode())
    assert result["converted_from"] == "ipynb" and result["cells"] == 2
    assert result["text"] == ("[cell 1, markdown]\n# Revenue\nQ3 figures.\n\n[cell 2, code]\nprint(40 + 2)\n[output]\n42\n<Figure>")
    assert "iVBOR" not in result["text"]
    for name, data in (("broken.ipynb", b"{not json"), ("empty.ipynb", b'{"cells": []}')):
        with pytest.raises(RunFailure) as raised:
            _write(tmp_path, name, data)
        assert raised.value.code == "source_unavailable"
