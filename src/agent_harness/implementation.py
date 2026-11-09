"""Reproducible identity for a package's source and shipped resources."""

import hashlib
import json
from pathlib import Path


def source_identity(package: Path) -> dict[str, str]:
    files = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(package.rglob("*"))
             if path.is_file() and "__pycache__" not in path.relative_to(package).parts
             and path.suffix not in {".pyc", ".pyo"}}
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"algorithm": "package-files-sha256-v1", "source_sha256": hashlib.sha256(encoded).hexdigest()}
