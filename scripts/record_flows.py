"""Re-record the browser flows shown in the README and make their previews.

    uv sync --group e2e
    uv run playwright install chromium ffmpeg      # or pass --channel msedge to use an installed Edge
    uv run python scripts/record_flows.py [--channel msedge] [-k pattern] [--out DIR]
    uv run python scripts/record_flows.py --from-videos DIR     # only rebuild the previews from existing videos

The flows are tests/test_browser_flows.py with the same scripted model as in CI, run slowly so they can be
followed. The videos go to DIR (default `.tmp/recordings/`, outside version control); the repository keeps only
previews in docs/previews/: a still of each flow's last frame, and a small animated GIF for the featured flows.
CI keeps each run's videos as the browser-flow-videos artifact. A flow that fails is reported and gets no new preview.
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
PREVIEWS = ROOT / "docs" / "previews"
SLOW = {"E2E_SLOW_MO": "250", "E2E_HOLD_MS": "1500", "E2E_REPLY_DELAY": "0.7"}
# Shown animated in the README; the others get a still. Kept few so the repository stays small.
FEATURED = ("01", "02", "07", "08")


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


def frames(ffmpeg: str, video: Path, fps: float, width: int) -> list[Image.Image]:
    with tempfile.TemporaryDirectory() as work:
        # Playwright's ffmpeg is a minimal build without the fps and scale filters: set the output rate, scale in Pillow.
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-r", str(fps), str(Path(work) / "f%04d.png")], check=True)
        result = []
        for path in sorted(Path(work).glob("f*.png")):
            with Image.open(path) as frame:
                scale = min(1, width / frame.width)  # never enlarge a small recording
                result.append(frame.convert("RGB").resize((round(frame.width * scale), round(frame.height * scale)), Image.Resampling.LANCZOS))
    if not result:
        raise RuntimeError(f"No frames in {video}")
    return result


def previews(ffmpeg: str, video: Path) -> list[Path]:
    """A still of the last frame for every flow, and for a featured one a GIF: 4 frames a second, 560 pixels wide."""
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    made = []
    still = PREVIEWS / f"{video.stem}.png"
    frames(ffmpeg, video, 1, 480)[-1].save(still, optimize=True)
    made.append(still)
    if video.stem[:2] in FEATURED:
        clip = frames(ffmpeg, video, 4, 560)
        palette = clip[-1].quantize(colors=64, method=Image.Quantize.MEDIANCUT)  # the interface colours barely change
        quantized = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in clip]
        target = PREVIEWS / f"{video.stem}.gif"
        quantized[0].save(target, save_all=True, append_images=quantized[1:], loop=0, optimize=True, disposal=1,
                          duration=[250] * (len(quantized) - 1) + [2500])
        made.append(target)
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="An installed browser to use instead of Playwright's Chromium, e.g. msedge.")
    parser.add_argument("-k", dest="pattern", help="Only the flows whose test name matches (pytest -k).")
    parser.add_argument("--out", type=Path, default=ROOT / ".tmp" / "recordings", help="Where the videos go.")
    parser.add_argument("--from-videos", type=Path, help="Skip recording; make the previews from the videos in this folder.")
    arguments = parser.parse_args()
    ffmpeg = find_ffmpeg()
    if arguments.from_videos:
        videos = sorted(video for video in arguments.from_videos.glob("[0-9][0-9]-*.webm") if "." not in video.stem)
        for video in videos:
            for made in previews(ffmpeg, video):
                print(f"{made.relative_to(ROOT)}  {made.stat().st_size / 1e3:.0f} kB")
        return 0 if videos else 1
    with tempfile.TemporaryDirectory() as recorded:
        environment = {**os.environ, **SLOW, "E2E_VIDEO_DIR": recorded, "REQUIRE_BROWSER_TESTS": "1", "E2E_BROWSERS": "chromium"}
        if arguments.channel:
            environment["E2E_BROWSER_CHANNEL"] = arguments.channel
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_browser_flows.py"]
        if arguments.pattern:
            command += ["-k", arguments.pattern]
        result = subprocess.run(command, cwd=ROOT, env=environment)
        failed = sorted(video.name.removesuffix(".failed.webm") for video in Path(recorded).glob("*.failed.webm"))
        arguments.out.mkdir(parents=True, exist_ok=True)
        kept = 0
        for video in sorted(Path(recorded).glob("*.webm")):
            if video.name.endswith(".failed.webm"):
                continue
            target = arguments.out / video.name
            shutil.move(str(video), target)
            made = previews(ffmpeg, target)
            kept += 1
            print(f"{target}  {target.stat().st_size / 1e6:.1f} MB; " + ", ".join(f"{path.name} {path.stat().st_size / 1e3:.0f} kB" for path in made))
    if failed:
        print("Failed, previews not updated: " + ", ".join(failed))
    print(f"{kept} recordings made.")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
