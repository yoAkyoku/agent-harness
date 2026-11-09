"""Uploads: save a file the user sends into the workspace's `uploads/` folder, where it becomes a source.

Everything is checked before a byte is written: a plain file name with a source type, the
type's size limit, and readability the way the file will be read as a source. Saving never
overwrites: identical bytes under an existing name return the saved file, different bytes are
refused. The file is written under a hidden temporary name and linked into place, so a
concurrent upload of the same name cannot replace it and a half-written file is never listed.
"""

import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import RunFailure
from .sources import DOCUMENT_EXTENSIONS, EXTENSIONS, document_text, file_limit

FOLDER = "uploads"
MAX_NAME = 120
FORBIDDEN = set('<>:"/\\|?*')
RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}


def upload_limit(name: str) -> int:
    """Validate the file name and return the size limit for its type."""
    suffix = Path(name).suffix.lower()
    if (not name or len(name) > MAX_NAME or name != name.strip() or name.startswith(".") or name.endswith(".")
            or any(character in FORBIDDEN or ord(character) < 32 or ord(character) == 127 for character in name)
            or suffix not in EXTENSIONS or Path(name).stem.lower() in RESERVED):
        raise RunFailure("invalid_upload_name", "Use a plain file name (no folders) ending in "
                         + ", ".join(sorted(EXTENSIONS)) + ".")
    return file_limit(suffix)


def save_upload(root: Path, name: str, raw: bytes) -> dict[str, Any]:
    limit = upload_limit(name)
    if len(raw) > limit:
        raise RunFailure("source_too_large", "The file exceeds the size limit for its type.")
    suffix = Path(name).suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        document_text(suffix, raw)
    else:
        try:
            raw.decode("utf-8")
        except UnicodeError as error:
            raise RunFailure("source_unavailable", "The file is not UTF-8 text.") from error
    folder = root / FOLDER
    if folder.is_symlink() or folder.is_junction() or (folder.exists() and not folder.is_dir()):
        raise RunFailure("uploads_unavailable", f"The workspace's {FOLDER} path is not a plain folder.")
    record = {"path": f"{FOLDER}/{name}", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    target = folder / name
    temporary = folder / f".{name}.{uuid4().hex}.part"
    try:
        folder.mkdir(exist_ok=True)
        try:
            temporary.write_bytes(raw)
            try:
                os.link(temporary, target)  # fails if the name exists, on Windows and POSIX alike
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or target.read_bytes() != raw:
                    raise RunFailure("upload_exists", f"{record['path']} already exists with different content.") from None
                return {**record, "created": False}
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:  # for example a workspace mounted read-only (docker -v ...:ro)
        raise RunFailure("uploads_unavailable", "The workspace folder cannot be written to; it may be read-only "
                         f"({error.strerror or type(error).__name__}).") from error
    return {**record, "created": True}
