"""Re-record the browser flows shown in the README: run them slowly, keep the videos, make the GIF previews.

    uv sync --group e2e
    uv run playwright install chromium ffmpeg      # or pass --channel msedge to use an installed Edge
    uv run python scripts/record_flows.py [--channel msedge] [-k pattern]

The flows are tests/test_browser_flows.py with the same scripted model as in CI. The videos go to
docs/videos/<name>.webm and each gets docs/videos/<name>.gif (5 frames per second, 800 pixels wide), because
GitHub offers .webm files only as a download. A flow that fails is reported and its old recording is kept.
"""
import argparse
import glob
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
VIDEOS = ROOT / "docs" / "videos"
SLOW = {"E2E_SLOW_MO": "250", "E2E_HOLD_MS": "1500", "E2E_REPLY_DELAY": "0.7"}


def find_ffmpeg() -> str:
    """Playwright's own ffmpeg (installed with `playwright install ffmpeg`), else one on PATH."""
    caches = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
              os.path.expanduser("~/.cache/ms-playwright"), os.path.expanduser("~/Library/Caches/ms-playwright")]
    for cache in filter(None, caches):
        found = sorted(glob.glob(os.path.join(cache, "ffmpeg-*", "ffmpeg*")), reverse=True)
        if found:
            return found[0]
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    sys.exit("No ffmpeg found: run `uv run playwright install ffmpeg`.")


def gif(ffmpeg: str, video: Path, fps: float = 5, width: int = 800) -> Path:
    """An animated preview: one palette for the whole clip (the interface colours barely change), a pause at the end."""
    with tempfile.TemporaryDirectory() as work:
        # Playwright's ffmpeg is a minimal build without the fps and scale filters: set the output rate, scale in Pillow.
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-r", str(fps), str(Path(work) / "f%04d.png")], check=True)
        frames = []
        for path in sorted(Path(work).glob("f*.png")):
            with Image.open(path) as frame:
                scale = min(1, width / frame.width)  # never enlarge a small recording
                frames.append(frame.convert("RGB").resize((round(frame.width * scale), round(frame.height * scale)), Image.Resampling.LANCZOS))
    if not frames:
        raise RuntimeError(f"No frames in {video}")
    palette = frames[-1].quantize(colors=96, method=Image.Quantize.MEDIANCUT)
    quantized = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    target = video.with_suffix(".gif")
    quantized[0].save(target, save_all=True, append_images=quantized[1:], loop=0, optimize=True, disposal=1,
                      duration=[int(1000 / fps)] * (len(quantized) - 1) + [2500])
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="An installed browser to use instead of Playwright's Chromium, e.g. msedge.")
    parser.add_argument("-k", dest="pattern", help="Only the flows whose test name matches (pytest -k).")
    arguments = parser.parse_args()
    ffmpeg = find_ffmpeg()
    with tempfile.TemporaryDirectory() as recorded:
        environment = {**os.environ, **SLOW, "E2E_VIDEO_DIR": recorded, "REQUIRE_BROWSER_TESTS": "1"}
        if arguments.channel:
            environment["E2E_BROWSER_CHANNEL"] = arguments.channel
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_browser_flows.py"]
        if arguments.pattern:
            command += ["-k", arguments.pattern]
        result = subprocess.run(command, cwd=ROOT, env=environment)
        failed = sorted(video.name.removesuffix(".failed.webm") for video in Path(recorded).glob("*.failed.webm"))
        VIDEOS.mkdir(parents=True, exist_ok=True)
        kept = 0
        for video in sorted(Path(recorded).glob("*.webm")):
            if video.name.endswith(".failed.webm"):
                continue
            target = VIDEOS / video.name
            shutil.move(str(video), target)
            preview = gif(ffmpeg, target)
            kept += 1
            print(f"{target.relative_to(ROOT)}  {target.stat().st_size / 1e6:.1f} MB, {preview.name} {preview.stat().st_size / 1e6:.1f} MB")
    if failed:
        print("Failed, old recordings kept: " + ", ".join(failed))
    print(f"{kept} recordings updated.")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
