"""How a workbench reads scanned PDFs and images: OCR, a vision model, or both.

One `Extraction` belongs to one app (it is passed to every source read), so two workbenches in the
same process can be configured differently and nothing here is module state.

OCR is Tesseract, either a local program (`LocalTesseract`, given the image as a file) or the same
program in a locked-down container (`DockerTesseract`, given the image on stdin: no network, no
writable file system, no capabilities). A vision model (`vision.VisionTranscriber`) reads what OCR
misses, but its text is model output: it is always labelled as a transcription, and when OCR ran on
the same image or page, the numbers the model wrote that OCR did not find are listed.
"""
from __future__ import annotations

import hashlib
import struct
import subprocess
import tempfile
import uuid
import zlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .contracts import RunFailure

if TYPE_CHECKING:
    from .vision import VisionTranscriber

Record = dict[str, Any]
OCR_DPI = 300  # Tesseract's preferred resolution
VISION_DPI = 150  # enough for a vision model, and a quarter of the pixels
OCR_PAGE_TIMEOUT_SECONDS = 60
MAX_OCR_PAGES = 30
MAX_VISION_PAGES = 10  # each page is one model call
MAX_CACHED = 32
MAX_SOURCE_BYTES = 262_144
# (argv, stdin, timeout seconds) -> (exit status, or None on timeout; stdout; stderr)
ProcessRunner = Callable[[list[str], bytes, float], tuple[int | None, bytes, bytes]]


def run_process(argv: list[str], stdin: bytes, timeout: float) -> tuple[int | None, bytes, bytes]:
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, b"", b""
    return done.returncode, done.stdout, done.stderr


class OcrEngine(Protocol):
    """Reads the text of one image; `identity` distinguishes configurations in the cache."""

    identity: tuple[str, ...]

    def status(self) -> Record: ...

    def read(self, image: bytes, suffix: str, label: str) -> str: ...


class LocalTesseract:
    """A Tesseract program on this computer, run as `<program> <image file> stdout -l <languages>`."""

    def __init__(self, command: list[str], languages: str = "eng") -> None:
        self.command, self.languages = tuple(command), languages
        self.identity: tuple[str, ...] = ("local", *self.command, languages)

    def status(self) -> Record:
        return {"enabled": True, "languages": self.languages}

    def read(self, image: bytes, suffix: str, label: str) -> str:
        with tempfile.TemporaryDirectory(prefix="agent-harness-ocr-") as folder:
            path = Path(folder) / f"image{suffix}"
            path.write_bytes(image)
            try:
                code, stdout, _ = run_process([*self.command, str(path), "stdout", "-l", self.languages], b"",
                                              OCR_PAGE_TIMEOUT_SECONDS)
            except OSError as error:
                raise RunFailure("source_unavailable", f"OCR failed on {label}: {type(error).__name__}.") from error
        return _ocr_output(code, stdout, label)


class DockerTesseract:
    """Tesseract in a container per image: the image goes in on stdin, and the container can reach nothing."""

    def __init__(self, image: str, languages: str = "eng", *, docker: str = "docker",
                 runner: ProcessRunner | None = None) -> None:
        self.image, self.languages, self.docker = image, languages, docker
        self._runner = runner or run_process
        self.identity: tuple[str, ...] = ("docker", image, languages)

    def status(self) -> Record:
        return {"enabled": True, "languages": self.languages, "docker_image": self.image}

    def check(self) -> None:
        """Fail at startup, not on the first scanned page, if Docker or the image is missing."""
        try:
            code, _, stderr = self._runner([self.docker, "image", "inspect", "--format", "{{.Id}}", self.image], b"", 30.0)
        except OSError as error:
            raise RuntimeError(f"--tesseract-docker: {self.docker!r} could not be started: {type(error).__name__}.") from error
        if code != 0:
            raise RuntimeError(f"--tesseract-docker: image {self.image!r} is not available to {self.docker!r} "
                               f"(pull or build it first): {stderr.decode('utf-8', 'replace')[:200]}")

    def command(self, name: str) -> list[str]:
        return [self.docker, "run", "--rm", "--name", name, "--pull", "never", "--interactive",
                "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                self.image, "tesseract", "stdin", "stdout", "-l", self.languages]

    def read(self, image: bytes, suffix: str, label: str) -> str:
        name = f"agent-harness-ocr-{uuid.uuid4().hex[:12]}"
        try:
            code, stdout, _ = self._runner(self.command(name), image, OCR_PAGE_TIMEOUT_SECONDS)
            if code is None:  # the docker client was stopped; make sure the container is too
                self._runner([self.docker, "kill", name], b"", 30.0)
        except OSError as error:
            raise RunFailure("source_unavailable", f"OCR failed on {label}: {type(error).__name__}.") from error
        return _ocr_output(code, stdout, label)


def _ocr_output(code: int | None, stdout: bytes, label: str) -> str:
    if code is None:
        raise RunFailure("source_unavailable", f"OCR failed on {label}: it did not finish in {OCR_PAGE_TIMEOUT_SECONDS} seconds.")
    if code != 0:
        raise RunFailure("source_unavailable", f"OCR failed on {label} (exit status {code}).")
    return stdout.decode("utf-8", errors="replace").strip()


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def gray_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """An 8-bit grayscale PNG from rows of `width` bytes (no imaging library needed)."""
    raw = b"".join(b"\0" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))


def _bounded(parts: list[str]) -> str:
    text = "\n\n".join(parts)
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise RunFailure("source_too_large", "The document's extracted text exceeds the 256 KiB limit.")
    return text


@dataclass
class Extraction:
    """The OCR engine and vision model one workbench uses, with their results cached by file hash."""

    ocr: OcrEngine | None = None
    vision: "VisionTranscriber | None" = None
    _cache: dict[tuple[Any, ...], tuple[str, Record]] = field(default_factory=dict, repr=False)

    def status(self) -> Record:
        return self.ocr.status() if self.ocr is not None else {"enabled": False}

    def _key(self, kind: str, raw: bytes) -> tuple[Any, ...]:
        return (kind, hashlib.sha256(raw).hexdigest(), self.ocr.identity if self.ocr else (),
                self.vision.identity if self.vision else ())

    def _remember(self, key: tuple[Any, ...], text: str, details: Record) -> tuple[str, Record]:
        if len(self._cache) >= MAX_CACHED:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (text, details)
        return text, deepcopy(details)

    def _transcribe(self, image: bytes, where: str, ocr: str | None, record: Record) -> str | None:
        """One labelled transcription section, or None when the model found no text; updates `record`."""
        from .vision import unconfirmed_numbers

        assert self.vision is not None
        transcription = self.vision.transcribe(image)
        if not transcription:
            return None
        label = f"[{where}, model transcription by {self.vision.model}; not verbatim source text"
        record.update({"kind": "model_transcription", "model": self.vision.model, "digest": self.vision.digest})
        if ocr is not None:
            unconfirmed = unconfirmed_numbers(transcription, ocr)
            record.setdefault("unconfirmed_numbers", [])
            record["unconfirmed_numbers"] += [number for number in unconfirmed if number not in record["unconfirmed_numbers"]]
            if unconfirmed:
                label += "; numbers not found by OCR: " + ", ".join(unconfirmed)
        return f"{label}]\n{transcription}"

    def image_text(self, suffix: str, raw: bytes) -> tuple[str, Record]:
        """An image source: OCR text, a labelled model transcription, or both."""
        if self.ocr is None and self.vision is None:
            raise RunFailure("source_unavailable", "An image is read through OCR or a vision model; start the workbench "
                             "with --tesseract or --vision-model to read it.")
        key = self._key("image", raw)
        if key in self._cache:
            return self._cache[key][0], deepcopy(self._cache[key][1])
        details: Record = {"converted_from": suffix.lstrip(".")}
        parts: list[str] = []
        ocr: str | None = None
        if self.ocr is not None:
            ocr = self.ocr.read(raw, suffix, "the image")
            if ocr:
                parts.append(f"[image, OCR]\n{ocr}")
                details["ocr"] = "tesseract"
        if self.vision is not None:
            record: Record = {}
            section = self._transcribe(raw, "image", ocr, record)
            if section is not None:
                parts.append(section)
                details["transcription"] = record
        if not parts:
            engines = " or ".join(name for name, on in (("OCR", self.ocr), ("the vision model", self.vision)) if on)
            raise RunFailure("source_unavailable", f"{engines} found no text in the image.")
        return self._remember(key, _bounded(parts), details)

    def scanned_pdf_text(self, raw: bytes, count: int) -> tuple[str, Record]:
        """A PDF with no text layer: each page is rendered and read by OCR, a vision model, or both."""
        if self.ocr is None and self.vision is None:
            raise RunFailure("source_unavailable", "The PDF has no extractable text; a scanned PDF needs OCR "
                             "(start the workbench with --tesseract or --vision-model to read it).")
        if count > MAX_OCR_PAGES:
            raise RunFailure("source_too_large", f"OCR is limited to {MAX_OCR_PAGES} pages.")
        if self.vision is not None and count > MAX_VISION_PAGES:
            raise RunFailure("source_too_large", f"A vision model reads scanned PDFs of at most {MAX_VISION_PAGES} pages.")
        key = self._key("pdf", raw)
        if key in self._cache:
            return self._cache[key][0], deepcopy(self._cache[key][1])
        import pypdfium2

        details: Record = {"converted_from": "pdf", "pages": count}
        parts: list[str] = []
        record: Record = {}
        found = False
        document = pypdfium2.PdfDocument(raw)
        try:
            for number in range(1, count + 1):
                ocr: str | None = None
                if self.ocr is not None:
                    width, height, rows = _render(document[number - 1], OCR_DPI)
                    ocr = self.ocr.read(f"P5 {width} {height} 255\n".encode() + b"".join(rows), ".pgm", f"page {number}")
                    parts.append(f"[page {number}, OCR]\n{ocr}")
                    found = found or bool(ocr)
                if self.vision is not None:
                    width, height, rows = _render(document[number - 1], VISION_DPI)
                    section = self._transcribe(gray_png(width, height, rows), f"page {number}", ocr, record)
                    if section is not None:
                        parts.append(section)
                        found = True
                _bounded(parts)  # stop early on a huge document
        finally:
            document.close()
        if not found:
            engines = " or ".join(name for name, on in (("OCR", self.ocr), ("the vision model", self.vision)) if on)
            raise RunFailure("source_unavailable", f"{engines} found no text in the scanned PDF.")
        if self.ocr is not None:
            details["ocr"] = "tesseract"
        if record:
            details["transcription"] = record
        return self._remember(key, _bounded(parts), details)


def _render(page: Any, dpi: int) -> tuple[int, int, list[bytes]]:
    """One PDF page as grayscale rows at the given resolution."""
    bitmap = page.render(scale=dpi / 72, grayscale=True)
    width, height, stride = bitmap.width, bitmap.height, bitmap.stride
    pixels = bytes(bitmap.buffer)
    return width, height, [pixels[row * stride: row * stride + width] for row in range(height)]
