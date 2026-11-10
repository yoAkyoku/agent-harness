"""A bounded catalog of text sources inside an explicit workspace.

Text files are read as UTF-8. PDF, Word, Excel and PowerPoint files are sources too, and so are
images when OCR is configured: the model sees their extracted text, while the source's SHA-256
and size stay those of the file's bytes, so a changed document is detected like any changed source.

Tables (CSV, and each sheet of an .xlsx workbook) can be far larger than what a model should read:
the table tools compute over the whole table on the server (`read_table`), while `read_source`
gives the model a labelled preview of a large table instead of cutting it silently.
"""

import csv
from datetime import date, datetime, time
import os
import io
import json
import re
import hashlib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .contracts import RunFailure

TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json"}
TABLE_EXTENSIONS = {".csv", ".xlsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", *IMAGE_EXTENSIONS}
EXTENSIONS = TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS
MAX_SOURCE_BYTES = 262_144  # a text file, the text extracted from a document, or what the model reads of a table
MAX_TABLE_BYTES = 20 * 1024 * 1024  # a CSV file or .xlsx workbook, computed on the server
MAX_TABLE_ROWS = 200_000
PREVIEW_ROWS = 20  # rows of a large table shown to the model
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_PAGES = 200
MAX_DOCUMENT_UNPACKED_BYTES = 50 * 1024 * 1024  # a .docx is a zip archive; refuse ones that inflate beyond this
# Optional OCR for PDFs without a text layer (configure_ocr); off unless the workbench names a Tesseract.
OCR_COMMAND: tuple[str, ...] | None = None
OCR_LANGUAGES = "eng"
OCR_SCALE = 300 / 72  # render at 300 dpi, Tesseract's preferred resolution
OCR_PAGE_TIMEOUT_SECONDS = 60
MAX_OCR_PAGES = 30
_OCR_CACHE: dict[tuple[str, tuple[str, ...], str], str] = {}
MAX_SOURCES = 8
MAX_SOURCE_PATH = 500
MAX_LABELLED_LINES = 16
MAX_LABELLED_VIEW_BYTES = 2048


def _colon_line_candidates(text: str) -> Iterator[dict[str, Any]]:
    for number, line in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r"([^:：\r\n]+)[:：]([ \t]*)(.*)", line)
        if match is not None and match[1].strip():
            item: dict[str, Any] = {"line": number, "label": match[1].strip(), "value": match[3]}
            if not match[2] and match[3]:
                item["compact"] = True
            if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", line.lstrip()):
                item["ambiguous_uri"] = True
            yield item


def labelled_source_lines(text: str) -> list[dict[str, Any]]:
    """Conservative padded/empty colon lines; explicit queries also accept compact lines."""
    result = []
    for item in _colon_line_candidates(text):
        if item.get("ambiguous_uri") or item.get("compact"):
            continue
        result.append(item)
        if len(result) > MAX_LABELLED_LINES or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_LABELLED_VIEW_BYTES:
            return []
    return result


def query_source_labels(text: str, labels: list[str]) -> dict[str, Any]:
    """Query exact literal labels; overflow never produces an absence claim."""
    matches: dict[str, list[dict[str, Any]]] = {label: [] for label in labels}
    count = 0
    for item in _colon_line_candidates(text):
        if item["label"] in matches:
            if item.get("ambiguous_uri"):
                return {"complete": False, "matches": None}
            matches[item["label"]].append({"line": item["line"], "value": item["value"]})
            count += 1
            if count > MAX_LABELLED_LINES:
                return {"complete": False, "matches": None}
    result = {"complete": True, "matches": matches}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_LABELLED_VIEW_BYTES:
        return {"complete": False, "matches": None}
    return result


def workspace_root(value: str) -> Path:
    try:
        root = Path(value)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError
        return root.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise RunFailure("invalid_workspace", "Choose an existing absolute workspace directory.") from error


def catalog(root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    visited = 0
    for folder, directories, filenames in os.walk(root, followlinks=False):
        parent = Path(folder)
        directories[:] = sorted(
            name for name in directories
            if not name.startswith(".")
            and not (parent / name).is_symlink()
            and not (parent / name).is_junction()
            and len((parent / name).relative_to(root).parts) < 6
        )
        for name in sorted(filenames):
            visited += 1
            if visited > 2000 or len(result) >= 200:
                return result
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if (
                name.startswith(".") or path.suffix.lower() not in EXTENSIONS
                or path.is_symlink() or len(relative) > MAX_SOURCE_PATH
            ):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= file_limit(path.suffix.lower()):
                result.append({"path": relative, "bytes": size})
    return result


def _read_raw(root: Path, relative: str) -> tuple[str, bytes]:
    """(suffix, bytes) of a source inside the workspace, refusing anything outside its scope or limit."""
    path = root / relative
    resolved = path.resolve(strict=True)
    parts = Path(relative).parts
    if (
        not resolved.is_relative_to(root) or not parts
        or Path(relative).is_absolute() or any(part.startswith(".") for part in parts)
        or resolved.suffix.lower() not in EXTENSIONS
        or any((root.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1))
    ):
        raise RunFailure("source_forbidden", "The requested source is outside the allowed workspace scope.")
    suffix = resolved.suffix.lower()
    limit = file_limit(suffix)
    with resolved.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise RunFailure("source_too_large", f"The selected source exceeds the {_size(limit)} limit.")
    return suffix, raw


def read_source(root: Path, relative: str) -> dict[str, Any]:
    """What the model reads of a source: its text, or a labelled preview of a large table."""
    try:
        suffix, raw = _read_raw(root, relative)
        identity = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        if suffix in DOCUMENT_EXTENSIONS:
            text, details = document_text(suffix, raw)
            return {"path": relative, "text": text, **identity, **details}
        text = raw.decode("utf-8")
        if suffix == ".csv":
            text, details = _table_view(text)
            return {"path": relative, "text": text, **identity, **details}
        return {"path": relative, "text": text, **identity}
    except (OSError, UnicodeError, ValueError) as error:
        raise RunFailure("source_unavailable", "The selected source cannot be read as UTF-8 text.") from error


def read_table(root: Path, relative: str, sheet: str | None = None) -> dict[str, Any]:
    """The whole table as CSV text, for the table tools: a CSV file, or one sheet of a workbook."""
    try:
        suffix, raw = _read_raw(root, relative)
        identity = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}
        if suffix == ".csv":
            if sheet is not None:
                raise RunFailure("invalid_arguments", "Only Excel workbooks have sheets; omit sheet for a CSV file.",
                                 correctable_fields=("sheet",))
            return {**identity, "text": raw.decode("utf-8")}
        if suffix == ".xlsx":
            try:
                names, sheets = _xlsx_sheets(raw)
            except RunFailure:
                raise
            except Exception as error:
                raise RunFailure("source_unavailable", "The XLSX file cannot be read; it may be damaged.") from error
            chosen = sheet if sheet is not None else names[0]
            if chosen not in sheets:
                raise RunFailure("invalid_table", f"No sheet named {chosen!r}; the workbook has: {', '.join(names)}.",
                                 correctable_fields=("sheet",))
            return {**identity, "text": _csv_text(sheets[chosen]), "sheet": chosen}
        raise RunFailure("invalid_arguments", "Table tools require a selected CSV or Excel file.")
    except (OSError, UnicodeError, ValueError) as error:
        raise RunFailure("source_unavailable", "The selected table cannot be read.") from error


def _csv_text(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def _table_view(text: str) -> tuple[str, dict[str, Any]]:
    """A CSV the model can read whole, or a labelled preview of its first rows."""
    if len(text.encode("utf-8")) <= MAX_SOURCE_BYTES:
        return text, {}
    rows = list(csv.reader(io.StringIO(text, newline="")))
    total = max(0, len(rows) - 1)
    shown = min(PREVIEW_ROWS, total)
    preview = (f"[preview: {shown} of {total} rows; compute with table_inspect and table_aggregate]\n"
               + _csv_text(rows[:1 + shown]))
    return preview, {"preview": True, "rows": total}


def file_limit(suffix: str) -> int:
    if suffix in TABLE_EXTENSIONS:
        return MAX_TABLE_BYTES
    return MAX_DOCUMENT_BYTES if suffix in DOCUMENT_EXTENSIONS else MAX_SOURCE_BYTES


def _size(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MiB" if limit >= 1024 * 1024 else f"{limit // 1024} KiB"


def _bounded_text(parts: list[str], separator: str) -> str:
    text = separator.join(parts)
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise RunFailure("source_too_large", f"The document's extracted text exceeds the {_size(MAX_SOURCE_BYTES)} limit.")
    return text


def document_text(suffix: str, raw: bytes) -> tuple[str, dict[str, Any]]:
    """Extract a document's text, bounded; refuse with a reason instead of returning nothing."""
    kind = suffix.lstrip(".")
    try:
        if suffix == ".pdf":
            return _pdf_text(raw)
        if suffix == ".xlsx":
            return _xlsx_text(raw)
        if suffix == ".pptx":
            return _pptx_text(raw)
        if suffix in IMAGE_EXTENSIONS:
            return _image_text(suffix, raw)
        return _docx_text(raw)
    except RunFailure:
        raise
    except Exception as error:  # parser libraries raise many types for damaged files
        raise RunFailure("source_unavailable", f"The {kind.upper()} file cannot be read; it may be damaged.") from error


def _pdf_text(raw: bytes) -> tuple[str, dict[str, Any]]:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise RunFailure("source_unavailable", "The PDF is encrypted; save an unprotected copy to use it as a source.")
    count = len(reader.pages)
    if count == 0:
        raise RunFailure("source_unavailable", "The PDF file cannot be read; it has no pages.")
    if count > MAX_DOCUMENT_PAGES:
        raise RunFailure("source_too_large", f"The PDF has more than {MAX_DOCUMENT_PAGES} pages.")
    pages, found = [], False
    for number, page in enumerate(reader.pages, 1):
        content = (page.extract_text() or "").strip()
        found = found or bool(content)
        pages.append(f"[page {number}]\n{content}")
        _bounded_text(pages, "\n\n")  # stop early on a huge document
    if not found:
        if OCR_COMMAND is None:
            raise RunFailure("source_unavailable", "The PDF has no extractable text; a scanned PDF needs OCR "
                             "(start the workbench with --tesseract to read it).")
        return _ocr_text(raw, count), {"converted_from": "pdf", "pages": count, "ocr": "tesseract"}
    return _bounded_text(pages, "\n\n"), {"converted_from": "pdf", "pages": count}


def configure_ocr(command: list[str] | None, languages: str = "eng") -> None:
    """Use this OCR program (Tesseract's command line) for PDFs without a text layer; None turns OCR off."""
    global OCR_COMMAND, OCR_LANGUAGES
    OCR_COMMAND, OCR_LANGUAGES = (tuple(command) if command else None), languages
    _OCR_CACHE.clear()


def _ocr_text(raw: bytes, count: int) -> str:
    """Render each page to a grayscale PGM image and read it with the OCR program; cached by file hash."""
    if count > MAX_OCR_PAGES:
        raise RunFailure("source_too_large", f"OCR is limited to {MAX_OCR_PAGES} pages.")
    assert OCR_COMMAND is not None
    key = (hashlib.sha256(raw).hexdigest(), OCR_COMMAND, OCR_LANGUAGES)
    if key in _OCR_CACHE:
        return _OCR_CACHE[key]
    import pypdfium2
    import tempfile

    pages: list[str] = []
    document = pypdfium2.PdfDocument(raw)
    try:
        with tempfile.TemporaryDirectory(prefix="agent-harness-ocr-") as folder:
            for number in range(1, count + 1):
                bitmap = document[number - 1].render(scale=OCR_SCALE, grayscale=True)
                width, height, stride = bitmap.width, bitmap.height, bitmap.stride
                pixels = bytes(bitmap.buffer)
                image = Path(folder) / f"page-{number}.pgm"
                image.write_bytes(f"P5 {width} {height} 255\n".encode() +
                                  b"".join(pixels[row * stride: row * stride + width] for row in range(height)))
                pages.append(f"[page {number}, OCR]\n{_run_ocr(image, f'page {number}')}")
                _bounded_text(pages, "\n\n")
    finally:
        document.close()
    if not any(page.split("\n", 1)[1] for page in pages):
        raise RunFailure("source_unavailable", "OCR found no text in the scanned PDF.")
    text = _bounded_text(pages, "\n\n")
    if len(_OCR_CACHE) >= 32:
        _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
    _OCR_CACHE[key] = text
    return text


def _run_ocr(image: Path, label: str) -> str:
    """Run the OCR program on one image file and return its text."""
    import subprocess

    assert OCR_COMMAND is not None
    try:
        done = subprocess.run([*OCR_COMMAND, str(image), "stdout", "-l", OCR_LANGUAGES], capture_output=True,
                              timeout=OCR_PAGE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunFailure("source_unavailable", f"OCR failed on {label}: {type(error).__name__}.") from error
    if done.returncode != 0:
        raise RunFailure("source_unavailable", f"OCR failed on {label} (exit status {done.returncode}).")
    return done.stdout.decode("utf-8", errors="replace").strip()


def _image_text(suffix: str, raw: bytes) -> tuple[str, dict[str, Any]]:
    """An image source is read through OCR, which the workbench enables with --tesseract."""
    if OCR_COMMAND is None:
        raise RunFailure("source_unavailable", "An image is read through OCR; start the workbench with --tesseract to read it.")
    key = (hashlib.sha256(raw).hexdigest(), OCR_COMMAND, OCR_LANGUAGES)
    if key not in _OCR_CACHE:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="agent-harness-ocr-") as folder:
            image = Path(folder) / f"image{suffix}"
            image.write_bytes(raw)
            content = _run_ocr(image, "the image")
        if not content:
            raise RunFailure("source_unavailable", "OCR found no text in the image.")
        if len(_OCR_CACHE) >= 32:
            _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
        _OCR_CACHE[key] = _bounded_text([f"[image, OCR]\n{content}"], "")
    return _OCR_CACHE[key], {"converted_from": suffix.lstrip("."), "ocr": "tesseract"}


def _check_archive(raw: bytes, label: str) -> None:
    """Office files are zip archives; refuse ones that inflate beyond the limit before parsing them."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_DOCUMENT_UNPACKED_BYTES:
            raise RunFailure("source_too_large",
                             f"The {label} file's unpacked size exceeds the {_size(MAX_DOCUMENT_UNPACKED_BYTES)} limit.")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _xlsx_sheets(raw: bytes) -> tuple[list[str], dict[str, list[list[str]]]]:
    """Every sheet's cell values (as last saved, not formulas), trimmed of empty trailing rows and columns."""
    import openpyxl

    _check_archive(raw, "Excel")
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        names = list(book.sheetnames)
        sheets: dict[str, list[list[str]]] = {}
        for name in names:
            rows: list[list[str]] = []
            for row in book[name].iter_rows(values_only=True):
                rows.append([_cell(value) for value in row])
                if len(rows) > MAX_TABLE_ROWS + 1:
                    raise RunFailure("source_too_large", f"Sheet {name!r} has more than {MAX_TABLE_ROWS} rows.")
            while rows and not any(rows[-1]):
                rows.pop()
            width = max((max((index + 1 for index, cell in enumerate(row) if cell), default=0) for row in rows), default=0)
            sheets[name] = [row[:width] + [""] * (width - len(row[:width])) for row in rows]
    finally:
        book.close()
    return names, sheets


def _xlsx_text(raw: bytes) -> tuple[str, dict[str, Any]]:
    """Each sheet as CSV under a [sheet NAME] line; a large workbook becomes a labelled preview per sheet."""
    names, sheets = _xlsx_sheets(raw)
    if not any(sheets.values()):
        raise RunFailure("source_unavailable", "The Excel file has no cell values.")
    full = "\n\n".join(f"[sheet {name}]\n{_csv_text(rows)}".rstrip("\n") for name, rows in sheets.items())
    if len(full.encode("utf-8")) <= MAX_SOURCE_BYTES:
        return full, {"converted_from": "xlsx", "sheets": names}
    parts = []
    for name, rows in sheets.items():
        total = max(0, len(rows) - 1)
        shown = min(PREVIEW_ROWS, total)
        parts.append(f"[sheet {name}: preview {shown} of {total} rows; compute with table_inspect and table_aggregate]\n"
                     + _csv_text(rows[:1 + shown]).rstrip("\n"))
    return _bounded_text(parts, "\n\n"), {"converted_from": "xlsx", "sheets": names, "preview": True}


def _pptx_text(raw: bytes) -> tuple[str, dict[str, Any]]:
    """Each slide's text (titles, text boxes, table rows) under a [slide N] line."""
    import pptx

    _check_archive(raw, "PowerPoint")
    slides: list[str] = []
    found = False
    for number, slide in enumerate(pptx.Presentation(io.BytesIO(raw)).slides, 1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines.extend(paragraph.text.strip() for paragraph in shape.text_frame.paragraphs if paragraph.text.strip())
            if getattr(shape, "has_table", False):
                lines.extend(" | ".join(cell.text.strip() for cell in row.cells) for row in shape.table.rows)
        found = found or bool(lines)
        slides.append("\n".join([f"[slide {number}]", *lines]))
        _bounded_text(slides, "\n\n")
    if not found:
        raise RunFailure("source_unavailable", "The PowerPoint file has no extractable text.")
    return _bounded_text(slides, "\n\n"), {"converted_from": "pptx", "slides": len(slides)}


def _docx_text(raw: bytes) -> tuple[str, dict[str, Any]]:
    import docx
    from docx.table import Table

    _check_archive(raw, "Word")
    lines: list[str] = []
    for block in docx.Document(io.BytesIO(raw)).iter_inner_content():
        if isinstance(block, Table):
            lines.extend(" | ".join(cell.text.strip() for cell in row.cells) for row in block.rows)
        elif block.text.strip():
            lines.append(block.text.strip())
        _bounded_text(lines, "\n")
    if not lines:
        raise RunFailure("source_unavailable", "The Word file has no extractable text.")
    return _bounded_text(lines, "\n"), {"converted_from": "docx"}


def admit_sources(root: Path, selected: list[str]) -> list[dict[str, Any]]:
    available = {item["path"] for item in catalog(root)}
    if len(set(selected)) != len(selected) or any(name not in available for name in selected):
        raise RunFailure("source_forbidden", "Select distinct files from the workspace source catalog.")
    return [{key: value for key, value in read_source(root, name).items() if key != "text"} for name in selected]


def relevant_excerpts(root: Path, sources: list[dict[str, Any]], task: str, budget: int) -> list[dict[str, Any]]:
    """Select whole matching lines from admitted versions; hints never grant citation IDs."""
    terms = {word.casefold() for word in re.findall(r"\w{3,}", task)}
    excerpts: list[dict[str, Any]] = []
    if not budget or not terms:
        return excerpts
    for source in sources:
        current = read_source(root, source["path"])
        if current["sha256"] != source["sha256"]:
            raise RunFailure("source_changed", "A selected source changed during context selection.")
        for number, line in enumerate(current["text"].splitlines(), 1):
            if not any(term in line.casefold() for term in terms):
                continue
            candidate = {"path": source["path"], "sha256": source["sha256"],
                         "start_line": number, "end_line": number, "text": line}
            if len(json.dumps(excerpts + [candidate], ensure_ascii=False).encode("utf-8")) <= budget:
                excerpts.append(candidate)
    return excerpts
