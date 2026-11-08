"""Source snapshots of earlier releases, used to test store and checkpoint upgrades.

Each zip is `git archive --format=zip <revision> src` of the named revision,
vendored so upgrade tests do not depend on the repository's git history.
"""
from pathlib import Path

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy"


def legacy_source_zip(revision: str) -> bytes:
    """Bytes of the vendored `src` archive for a revision (7-character prefix or longer)."""
    path = LEGACY / f"{revision[:7]}-src.zip"
    if not path.is_file():
        raise FileNotFoundError(f"No vendored source snapshot for {revision}; expected {path.name}.")
    return path.read_bytes()
