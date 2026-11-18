"""The pictures and recordings the READMEs point to exist, and every recording has its preview and is shown."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
READMES = [ROOT / "README.md", ROOT / "README.zh-TW.md"]
LINK = re.compile(r'(?:\]\(|src="|href=")(docs/[^)"#\s]+)')


def test_every_local_media_link_in_the_readmes_exists() -> None:
    for readme in READMES:
        links = LINK.findall(readme.read_text(encoding="utf-8"))
        assert links, readme.name
        missing = sorted({link for link in links if not (ROOT / link).is_file()})
        assert not missing, f"{readme.name} links to missing files: {missing}"


def test_each_recording_has_a_preview_and_is_shown_in_both_readmes() -> None:
    videos = sorted((ROOT / "docs" / "videos").glob("*.webm"))
    assert videos
    for video in videos:
        assert not video.name.endswith(".failed.webm"), f"a failed recording was kept: {video.name}"
        assert video.with_suffix(".gif").is_file(), f"no preview for {video.name}"
        for readme in READMES:
            text = readme.read_text(encoding="utf-8")
            assert f"docs/videos/{video.name}" in text and f'src="docs/videos/{video.stem}.gif"' in text, (readme.name, video.name)
