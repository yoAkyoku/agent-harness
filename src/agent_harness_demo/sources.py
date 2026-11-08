"""A bounded catalog of ordinary text sources inside an explicit workspace."""

import os
import json
import re
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .contracts import RunFailure

EXTENSIONS = {".md", ".txt", ".csv", ".json"}
MAX_SOURCE_BYTES = 262_144
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
            if size <= MAX_SOURCE_BYTES:
                result.append({"path": relative, "bytes": size})
    return result


def read_source(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        parts = Path(relative).parts
        if (
            not resolved.is_relative_to(root) or not parts
            or Path(relative).is_absolute() or any(part.startswith(".") for part in parts)
            or resolved.suffix.lower() not in EXTENSIONS
            or any((root.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1))
        ):
            raise RunFailure("source_forbidden", "The requested source is outside the allowed workspace scope.")
        with resolved.open("rb") as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise RunFailure("source_too_large", "The selected source exceeds the 256 KiB limit.")
        return {"path": relative, "text": raw.decode("utf-8"), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    except (OSError, UnicodeError, ValueError) as error:
        raise RunFailure("source_unavailable", "The selected source cannot be read as UTF-8 text.") from error


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
