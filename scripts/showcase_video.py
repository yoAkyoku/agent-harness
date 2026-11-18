"""Join the recorded browser flows into one 16:9 video with a title card before each, plus YouTube chapters.

    uv run python scripts/showcase_video.py VIDEO_DIR OUT_DIR [--channel msedge] [--card-seconds 4.5]

VIDEO_DIR holds the `NN-name.webm` files that `scripts/record_flows.py` (or CI's `browser-flow-videos` artifact)
produces. A browser plays them in order on one page while Playwright records that page, so nothing but Playwright
is needed. OUT_DIR gets `showcase.webm`, `chapters.txt` (one `m:ss title` line per flow, the format YouTube reads
from a description) and `description.md`. The recordings are not re-judged here: each card says what the flow's
test checks, in the words of the README.
"""
import argparse
import html
import json
from pathlib import Path
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright

SIZE = {"width": 1280, "height": 720}
CAPTIONS = {
    "01": "A plan is sent back with a note, the revised plan is approved, and the exact artifact is approved before it is saved.",
    "02": "An MCP tool with side effects waits for a single-use approval; a read-only MCP tool's result is cited as evidence.",
    "03": "A skill is picked for the task and loaded on request; the finished Run drafts a new SKILL.md.",
    "04": "A command hook refuses one source_read; the Run carries on with the other source.",
    "05": "A subagent and a source search answer a question directly, with verified citations.",
    "06": "Side panels, a resized panel remembered across a reload, the waiting-for-your-decision list and a weekly schedule.",
    "07": "The workbench process is killed mid-Run; after a restart the Run shows as interrupted and resumes from its checkpoint.",
    "08": "An instruction typed while the Run works reaches the model's next step; then the Run is cancelled.",
    "09": "A bad folder path, a claim citing evidence that was never produced, the correction limit and an out-of-scope read.",
    "10": "axe-core finds no WCAG A/AA violations in ten states of the workbench, and no untranslated text.",
    "11": "The Chinese interface, chosen from the browser's language, runs a task end to end.",
    "12": "Below 1180 pixels the preview moves under the task area.",
    "13": "At phone width the page is one column that scrolls down, never sideways, and a task still completes.",
}

PAGE = """<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0f1626;color:#eef2fb;font-family:"Segoe UI",system-ui,sans-serif;overflow:hidden}
#card{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;padding:0 120px}
#card .n{font-size:22px;color:#8fa6e0;letter-spacing:.08em}#card h1{font-size:46px;margin:14px 0 22px;font-weight:650}
#card p{font-size:24px;line-height:1.5;color:#c9d3ea;max-width:30em;margin:0}
video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#0f1626;display:none}
</style><div id="card"></div><video id="v" muted playsinline></video><script>
const items = __ITEMS__, CARD = __CARD__;
window.marks = []; window.finished = false;
const card = document.getElementById("card"), video = document.getElementById("v");
function show(item) {
  card.innerHTML = `<div class="n">${item.number}</div><h1>${item.title}</h1><p>${item.caption}</p>`;
  card.style.display = "flex"; video.style.display = "none";
}
async function run() {
  for (const item of items) {
    window.marks.push({title: item.chapter, at: performance.now()});
    show(item);
    await new Promise(done => setTimeout(done, CARD * 1000));
    if (!item.src) continue;
    video.src = item.src; card.style.display = "none"; video.style.display = "block";
    await new Promise(done => { video.onended = done; video.onerror = done; video.play(); });
  }
  window.finished = true;
}
run();
</script>"""


def stamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("videos", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--channel", help="An installed browser to use instead of Playwright's Chromium, e.g. msedge.")
    parser.add_argument("--card-seconds", type=float, default=4.5,
                        help="How long each title card shows (YouTube needs every chapter to last 10 s or more).")
    arguments = parser.parse_args()
    videos = sorted(arguments.videos.glob("[0-9][0-9]-*.webm"))
    videos = [video for video in videos if video.stem.count(".") == 0]  # not NN-name.firefox.webm or .failed.webm
    if not videos:
        sys.exit(f"No NN-name.webm recordings in {arguments.videos}.")
    items = [{"number": "Agent Harness", "title": "Thirteen recorded browser flows",
              "caption": "The model is a script; the workbench, the MCP server and the command hook are real processes. "
                         "Each card says what that flow's test checks.", "chapter": "Introduction", "src": None}]
    for video in videos:
        number, _, name = video.stem.partition("-")
        title = " ".join("MCP" if word == "mcp" else word for word in name.split("-")).capitalize().replace("Mcp", "MCP").replace(" mcp", " MCP")
        items.append({"number": f"Flow {number}", "title": html.escape(title), "caption": html.escape(CAPTIONS.get(number, "")),
                      "chapter": f"{number} {title}", "src": video.resolve().as_uri()})
    arguments.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        page_file = Path(scratch, "showcase.html")
        page_file.write_text(PAGE.replace("__ITEMS__", json.dumps(items)).replace("__CARD__", json.dumps(arguments.card_seconds)),
                             encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=arguments.channel, args=["--autoplay-policy=no-user-gesture-required"])
            context = browser.new_context(viewport=SIZE, record_video_dir=scratch, record_video_size=SIZE)
            page = context.new_page()
            page.goto(page_file.as_uri())
            page.wait_for_function("window.finished === true", timeout=30 * 60 * 1000, polling=500)
            page.wait_for_timeout(1000)
            marks = page.evaluate("window.marks")
            video = page.video
            context.close()
            browser.close()
            assert video is not None
            shutil.move(video.path(), arguments.out / "showcase.webm")
    # The recording starts when the page is created, a moment before the first mark; chapters are measured from it.
    first = marks[0]["at"]
    chapters = [f"{stamp((mark['at'] - first) / 1000 if index else 0)} {mark['title']}" for index, mark in enumerate(marks)]
    (arguments.out / "chapters.txt").write_text("\n".join(chapters) + "\n", encoding="utf-8")
    description = ("Thirteen browser flows of Agent Harness, recorded by its Playwright tests: plan and publish approval, "
                   "MCP tools, skills, hooks, subagents, crash and resume, steering, errors, accessibility, the Chinese "
                   "interface and narrow layouts. The model in these flows is a script; the workbench, the MCP server "
                   "and the command hook are real processes.\n\nSource: https://github.com/yoAkyoku/agent-harness\n\n"
                   + "\n".join(chapters) + "\n")
    (arguments.out / "description.md").write_text(description, encoding="utf-8")
    print("\n".join(chapters))
    return 0


if __name__ == "__main__":
    sys.exit(main())
