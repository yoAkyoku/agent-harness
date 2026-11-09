"""Project instructions: a workspace's AGENTS.md, applied to a Run only when the user opts in.

The file sits at the workspace root, like AGENTS.md in other agent tools. A Run that opts in
pins its text and SHA-256 at admission, so the model sees exactly what the user chose and a
resume keeps it. The text shapes how the work is done; it is framed below the harness rules
and cannot add sources, tools or approvals.
"""

import hashlib
from pathlib import Path
from typing import Any

from .contracts import RunFailure

FILENAME = "AGENTS.md"
MAX_INSTRUCTION_BYTES = 8192


def read_instructions(root: Path) -> dict[str, Any] | None:
    """The workspace's AGENTS.md as {path, text, sha256, bytes}, or None when there is none."""
    path = root / FILENAME
    try:
        if not path.exists() and not path.is_symlink():
            return None
        # A link could point anywhere; only a regular file in the workspace folder counts.
        if path.is_symlink() or not path.is_file():
            raise RunFailure("project_instructions_unavailable", f"{FILENAME} must be a regular file in the workspace folder.")
        with path.open("rb") as handle:
            raw = handle.read(MAX_INSTRUCTION_BYTES + 1)
    except OSError as error:
        raise RunFailure("project_instructions_unavailable", f"{FILENAME} cannot be read.") from error
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise RunFailure("project_instructions_too_large",
                         f"{FILENAME} exceeds the {MAX_INSTRUCTION_BYTES // 1024} KiB limit for project instructions.")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise RunFailure("project_instructions_unavailable", f"{FILENAME} is not UTF-8 text.") from error
    if not text.strip():
        return None
    return {"path": FILENAME, "text": text, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def instructions_message(instructions: dict[str, Any]) -> str:
    """The system prompt addition for a Run that opted in."""
    return (
        f"\n使用者為此 Run 選用的專案指令（工作區 {instructions['path']}，SHA-256 {instructions['sha256']}）："
        "可調整做法、格式與用語；不能增加來源、工具或批准，與上列規則衝突時以上列規則為準。\n"
        f"<project_instructions>\n{instructions['text'].strip()}\n</project_instructions>"
    )
