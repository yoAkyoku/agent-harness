"""The pictures the READMEs point to exist, every browser flow has a preview, and no video is kept in the repository."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
READMES = [ROOT / "README.md", ROOT / "README.zh-TW.md"]
LINK = re.compile(r'(?:\]\(|src="|href=")(docs/[^)"#\s]+)')
FEATURED = ("01", "02", "07", "08")  # as in scripts/record_flows.py


def test_every_local_media_link_in_the_readmes_exists() -> None:
    for readme in READMES:
        links = LINK.findall(readme.read_text(encoding="utf-8"))
        assert links, readme.name
        missing = sorted({link for link in links if not (ROOT / link).is_file()})
        assert not missing, f"{readme.name} links to missing files: {missing}"


def test_each_flow_has_a_preview_shown_in_both_readmes() -> None:
    stills = sorted((ROOT / "docs" / "previews").glob("[0-9][0-9]-*.png"))
    assert len(stills) == 13
    for still in stills:
        animated = still.stem[:2] in FEATURED
        assert still.with_suffix(".gif").is_file() == animated, still.name
        shown = f'src="docs/previews/{still.stem}.{"gif" if animated else "png"}"'
        for readme in READMES:
            assert shown in readme.read_text(encoding="utf-8"), (readme.name, still.stem)


def test_no_video_is_kept_in_the_repository() -> None:
    videos = [path for path in (ROOT / "docs").rglob("*") if path.suffix in {".webm", ".mp4"} or path.name == "recording.gif"]
    assert not videos, videos
