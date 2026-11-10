"""Image sources transcribed by a local vision model: optional, and labelled as model output.

A vision model reads charts, photos and handwriting better than Tesseract, but what it returns is
model output, not the file's text: it can misread a digit or add words that are not there. So it is
off unless the workbench names a model with --vision-model (an already-downloaded Ollama model that
advertises vision, checked at startup). Its text is marked as a model transcription with the model's
name, the source records which model and digest produced it, and both the evidence appendix of an
artifact and a verified answer say so. When Tesseract is configured too, both readings are given and
numbers the model wrote that OCR did not find are listed, never silently resolved either way.
These tests replace Ollama with a mock transport; the real model is exercised in docs/evaluation.
"""
import base64
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import sources
from agent_harness.contracts import RunFailure
from agent_harness.model import ModelSettings
from agent_harness.vision import unconfirmed_numbers
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_ocr import FAKE
from test_workbench import finish, tool_reply

PNG = b"\x89PNG\r\n\x1a\n" + b"\1" * 64
DIGEST = "a" * 64


class FakeOllama:
    """Answers /api/tags, /api/show and /api/chat like Ollama, recording each chat request."""

    def __init__(self, reply: str = "Revenue was 42.", *, capabilities: tuple[str, ...] = ("completion", "vision"),
                 status: int = 200) -> None:
        self.reply, self.capabilities, self.status = reply, capabilities, status
        self.chats: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "vision-model:latest", "digest": DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": list(self.capabilities)})
        assert request.url.path == "/api/chat"
        self.chats.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "model failed"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": self.reply}, "done": True})


def _client(tmp_path: Path, ollama: FakeOllama, replies: list[httpx.Response] | None = None, **options: object) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer(replies or [])),
                                 vision_model="vision-model:latest", vision_transport=httpx.MockTransport(ollama),
                                 **options))  # type: ignore[arg-type]


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    (root / "chart.png").write_bytes(PNG)
    return root


def _preview(client: TestClient, root: Path) -> httpx.Response:
    workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
    return client.get(f"/api/workspaces/{workspace['id']}/source", params={"path": "chart.png"})


def _run(client: TestClient, root: Path, **body: object) -> tuple[dict, list[dict]]:
    workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    run = client.post(f"/api/sessions/{session['id']}/runs",
                      json={"task": "What was the revenue?", "sources": ["chart.png"], **body})
    assert run.status_code == 202, run.text
    return finish(client, run.json())


def test_an_image_is_transcribed_and_labelled_as_model_output(tmp_path: Path) -> None:
    ollama = FakeOllama("Revenue was 42.\nCost was 17.")
    with _client(tmp_path, ollama) as client:
        preview = _preview(client, _workspace(tmp_path)).json()
        config = client.get("/api/config").json()

    assert preview["text"] == ("[image, model transcription by vision-model:latest; not verbatim source text]\n"
                               "Revenue was 42.\nCost was 17.")
    assert preview["converted_from"] == "png" and "ocr" not in preview
    assert preview["transcription"] == {"kind": "model_transcription", "model": "vision-model:latest", "digest": DIGEST}
    assert config["vision"] == {"enabled": True, "model": "vision-model:latest", "digest": DIGEST}
    chat = ollama.chats[0]
    assert chat["model"] == "vision-model:latest" and chat["stream"] is False and chat["think"] is False
    assert chat["options"]["temperature"] == 0
    assert base64.b64decode(chat["messages"][-1]["images"][0]) == PNG


def test_with_ocr_too_both_readings_are_given_and_unconfirmed_numbers_listed(tmp_path: Path) -> None:
    ollama = FakeOllama("Scanned revenue was 42 and cost was 1,081.")  # the fake OCR reads only "42"
    counter = tmp_path / "ocr-calls.txt"
    with _client(tmp_path, ollama, ocr_command=[sys.executable, FAKE, "text", str(counter)]) as client:
        preview = _preview(client, _workspace(tmp_path)).json()

    assert preview["text"] == (
        "[image, OCR]\nScanned revenue was 42. (eng)\n\n"
        "[image, model transcription by vision-model:latest; not verbatim source text; numbers not found by OCR: 1,081]\n"
        "Scanned revenue was 42 and cost was 1,081.")
    assert preview["ocr"] == "tesseract"
    assert preview["transcription"]["unconfirmed_numbers"] == ["1,081"]


def test_a_cited_transcription_is_disclosed_in_the_appendix_and_answer(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ollama = FakeOllama("Revenue was 42.")
    artifact_replies = [
        tool_reply("plan_update", {"steps": ["Read the chart", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "chart.png", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Revenue was 42. [chart.png E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ]
    with _client(tmp_path, ollama, artifact_replies) as client:
        final, events = _run(client, root)
        assert final["status"] == "completed", final["error"]
        artifact = final["artifacts"][0]
        content = client.get(f"/api/runs/{final['id']}/artifacts/{artifact['id']}").text
    assert "model transcription by vision-model:latest, not verbatim source text" in content
    assert len(ollama.chats) == 1  # admission, excerpts and the read share one transcription

    answer_replies = [
        tool_reply("plan_update", {"steps": ["Read the chart", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "chart.png", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [chart.png E1]."}, "finish"),
    ]
    with _client(tmp_path / "answer", FakeOllama("Revenue was 42."), answer_replies) as client:
        final, events = _run(client, root, completion_workflow="answer")
    assert final["status"] == "completed", final["error"]
    cited = next(event["data"] for event in events if event["kind"] == "answer.verified")["cited"]
    assert cited[0]["transcription"] == {"kind": "model_transcription", "model": "vision-model:latest", "digest": DIGEST}


def test_a_model_without_vision_stops_the_workbench_from_starting(tmp_path: Path) -> None:
    with pytest.raises(RunFailure) as raised:
        with _client(tmp_path, FakeOllama(capabilities=("completion", "tools"))):
            pass
    assert raised.value.code == "vision_model_unavailable" and "vision" in raised.value.message


def test_a_failed_transcription_is_refused_with_a_reason(tmp_path: Path) -> None:
    with _client(tmp_path, FakeOllama(status=500)) as client:
        refused = _preview(client, _workspace(tmp_path))
    assert refused.status_code == 422 and "vision model" in refused.json()["detail"]["message"]


def test_an_empty_transcription_is_refused(tmp_path: Path) -> None:
    with _client(tmp_path, FakeOllama("   ")) as client:
        refused = _preview(client, _workspace(tmp_path))
    assert refused.status_code == 422 and "no text" in refused.json()["detail"]["message"]


def test_without_ocr_or_a_vision_model_an_image_is_refused_naming_both(tmp_path: Path) -> None:
    sources.configure_vision(None)
    with pytest.raises(RunFailure) as raised:
        sources.document_text(".png", PNG)
    assert "--tesseract" in raised.value.message and "--vision-model" in raised.value.message


@pytest.mark.parametrize(("transcription", "ocr", "expected"), [
    ("Total 1,081 and 42.5%", "Total 1081 and 42.5", []),
    ("Total 1081", "Total 1031", ["1081"]),
    ("Q3 revenue 42", "", ["3", "42"]),
    ("no numbers", "1 2 3", []),
])
def test_numbers_are_compared_after_removing_thousands_separators(transcription: str, ocr: str, expected: list[str]) -> None:
    assert unconfirmed_numbers(transcription, ocr) == expected


@pytest.mark.parametrize("name", ["bad name!", "llava-cloud"])
def test_cli_validates_the_vision_model(tmp_path: Path, name: str) -> None:
    import subprocess

    done = subprocess.run([sys.executable, "-m", "agent_harness", "--store", str(tmp_path / "store"), "--vision-model", name],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "--vision-model must name an installed local Ollama model" in done.stderr
