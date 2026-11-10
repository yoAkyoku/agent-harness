"""Image transcription by a local vision model (Ollama), opt-in with --vision-model.

A vision model reads layouts, charts and handwriting that OCR misses, but its output is model text,
not the file's text, so the harness labels it as a transcription and records which model produced
it. `unconfirmed_numbers` compares its numbers with an OCR reading of the same image, when there is
one, so a misread digit is shown to the model and the user instead of being trusted silently.
"""
from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from .contracts import RunFailure

BASE_URL = "http://127.0.0.1:11434"
MODEL_NAME = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}"
TIMEOUT_SECONDS = 300
MAX_OUTPUT_TOKENS = 4096
PROMPT = ("Transcribe every piece of text visible in this image, exactly as written, in reading order. "
          "Keep numbers, units and punctuation as shown; write table rows as cells separated by ' | '. "
          "Do not translate, summarize, explain or add anything that is not visible. "
          "If a part is unreadable, write [unreadable] there. If there is no text, reply with nothing.")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def valid_model_name(name: str) -> bool:
    return re.fullmatch(MODEL_NAME, name) is not None and "cloud" not in name.lower()


def _numbers(text: str) -> list[str]:
    return [match.rstrip(",") for match in _NUMBER.findall(text)]


def unconfirmed_numbers(transcription: str, ocr: str) -> list[str]:
    """Numbers in the transcription (as written, in order, once each) that the OCR text does not contain."""
    found = {number.replace(",", "") for number in _numbers(ocr)}
    result: list[str] = []
    for number in _numbers(transcription):
        if number.replace(",", "") not in found and number not in result:
            result.append(number)
    return result


def transcription_note(evidence: dict[str, Any]) -> str:
    """For an evidence appendix line: say that the cited source text includes a model transcription."""
    record = evidence.get("transcription")
    if not isinstance(record, dict):
        return ""
    note = f"; includes a model transcription by {record.get('model')}, not verbatim source text"
    unconfirmed = record.get("unconfirmed_numbers")
    if unconfirmed:
        note += "; numbers not found by OCR: " + ", ".join(unconfirmed)
    return note


class VisionTranscriber:
    """Transcribes images with one already-downloaded local Ollama model that advertises vision."""

    def __init__(self, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not valid_model_name(model):
            raise ValueError("Choose a valid local vision model name.")
        self.model = model
        self.digest: str | None = None
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, trust_env=False, follow_redirects=False,
                                    timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5))

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "model": self.model, "digest": self.digest}

    @property
    def identity(self) -> tuple[str, str]:
        return self.model, self.digest or ""

    def check(self) -> None:
        """At startup: the model is installed locally with a digest and advertises vision; otherwise fail closed."""
        try:
            tags = self._client.get("/api/tags", timeout=10)
            show = self._client.post("/api/show", json={"model": self.model}, timeout=10)
            tags.raise_for_status()
            show.raise_for_status()
            found = next((item for item in tags.json()["models"] if item["name"] == self.model), None)
            metadata = show.json()
        except httpx.HTTPError as error:
            raise RunFailure("vision_model_unavailable",
                             "Start Ollama and install the --vision-model model before starting the workbench.") from error
        except (KeyError, TypeError, ValueError) as error:
            raise RunFailure("vision_model_unavailable", "The vision model's metadata could not be verified.") from error
        if metadata.get("remote_model") or metadata.get("remote_host"):
            raise RunFailure("vision_model_unavailable", "Only already-downloaded local model weights are supported.")
        if found is None or not re.fullmatch(r"[a-fA-F0-9]{64}", str(found.get("digest", ""))):
            raise RunFailure("vision_model_unavailable", f"The vision model {self.model} is not installed with a valid digest.")
        if "vision" not in (metadata.get("capabilities") or []):
            raise RunFailure("vision_model_unavailable", f"The model {self.model} does not advertise vision (image input).")
        self.digest = found["digest"]

    def transcribe(self, image: bytes) -> str:
        """The model's transcription of one image; refused with a reason when the model fails."""
        body = {"model": self.model, "stream": False, "think": False,
                "options": {"temperature": 0, "num_predict": MAX_OUTPUT_TOKENS},
                "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(image).decode("ascii")]}]}
        try:
            response = self._client.post("/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
            text = payload["message"]["content"]
            if not isinstance(text, str):
                raise TypeError
        except httpx.HTTPError as error:
            raise RunFailure("source_unavailable", f"The vision model could not read the image: {type(error).__name__}.") from error
        except (KeyError, TypeError, ValueError) as error:
            raise RunFailure("source_unavailable", "The vision model returned an unusable response.") from error
        if payload.get("done_reason") == "length":
            raise RunFailure("source_unavailable", "The vision model's transcription was cut off at its output limit.")
        return text.strip()

    def close(self) -> None:
        self._client.close()
