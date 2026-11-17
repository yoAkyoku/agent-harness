"""The workbench driven in a real browser, one recorded flow per theme.

Each test starts the workbench as a child process (tests/e2e_server.py) with a scripted model, opens it in
Chromium through Playwright, does what a person would do, and checks what the page shows. Every flow is
recorded; the videos are written to E2E_VIDEO_DIR (default .tmp/e2e-videos).

Playwright is not a default dependency: `uv sync --group e2e`, then `uv run playwright install chromium`
(or set E2E_BROWSER_CHANNEL=msedge / chrome to use an installed browser). Without it these tests are
skipped, unless REQUIRE_BROWSER_TESTS is set, as it is in CI.

E2E_SLOW_MO (ms between browser actions), E2E_HOLD_MS (pause on each state worth seeing) and
E2E_REPLY_DELAY (seconds before each scripted model reply) slow a run down so the videos can be followed.
"""
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest

if os.environ.get("REQUIRE_BROWSER_TESTS"):
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (uv sync --group e2e).")

Page = playwright_api.Page
expect = playwright_api.expect
SERVER = Path(__file__).with_name("e2e_server.py")
VIDEOS = Path(os.environ.get("E2E_VIDEO_DIR") or Path(__file__).resolve().parents[1] / ".tmp" / "e2e-videos")
HOLD_MS = int(os.environ.get("E2E_HOLD_MS", "0"))
SIZE = {"width": 1280, "height": 800}
expect.set_options(timeout=20_000)


@pytest.fixture(scope="module")
def browser() -> Iterator[playwright_api.Browser]:
    with playwright_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(channel=os.environ.get("E2E_BROWSER_CHANNEL") or None,
                                              slow_mo=int(os.environ.get("E2E_SLOW_MO", "0")))
        yield launched
        launched.close()


@contextmanager
def workbench(browser: playwright_api.Browser, tmp_path: Path, scenario: str, video: str) -> Iterator[tuple[Page, Path]]:
    """A running workbench for the scenario and a recorded page open on it; yields the page and the workspace folder."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log = tmp_path / "server.log"
    with log.open("wb") as output:
        process = subprocess.Popen([sys.executable, str(SERVER), scenario, str(tmp_path), str(port)], stdout=output,
                                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    context = None
    try:
        deadline = time.monotonic() + 60
        while True:
            assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-4000:]
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/config", trust_env=False, timeout=5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "The workbench did not start: " + log.read_text(encoding="utf-8", errors="replace")[-4000:]
            time.sleep(.1)
        context = browser.new_context(viewport=SIZE, locale="en-US", record_video_dir=str(tmp_path / "video"), record_video_size=SIZE)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        expect(page.locator("#mode-badge")).not_to_have_text("")
        yield page, tmp_path / "workspace"
        hold(page)
    finally:
        if context is not None:
            recorded = context.pages[0].video if context.pages else None
            context.close()  # the video file is complete only after the context closes
            if recorded is not None:
                VIDEOS.mkdir(parents=True, exist_ok=True)
                shutil.move(recorded.path(), VIDEOS / f"{video}.webm")
        process.terminate()
        process.wait(timeout=20)


def hold(page: Page) -> None:
    """Stay on a state worth seeing in the recording."""
    if HOLD_MS:
        page.wait_for_timeout(HOLD_MS)


def load_folder(page: Page, folder: Path, task: str) -> None:
    page.fill("#workspace-path", str(folder))
    page.click("#workspace-form button[type=submit]")
    boxes = page.locator("#sources input[type=checkbox]")
    expect(boxes.first).to_be_visible()
    for box in boxes.all():
        box.check()
    page.fill("#task-input", task)
    expect(page.locator("#start-task")).to_be_enabled()


def start(page: Page) -> None:
    hold(page)
    page.click("#start-task")
    expect(page.locator("#tab-run")).to_have_attribute("aria-selected", "true")


def approve(page: Page, shows: str) -> None:
    """The approval card shows exactly what is about to happen; approve it."""
    expect(page.locator("#approval-card")).to_be_visible()
    expect(page.locator("#approval-card")).to_contain_text(shows)
    hold(page)
    page.click("#approve-operation")


def completed(page: Page) -> None:
    expect(page.locator("#run-status")).to_have_class("run-status completed")
    expect(page.locator("#run-actions")).to_be_visible()


def test_a_plan_is_sent_back_then_approved_and_the_artifact_is_approved_before_it_is_saved(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "approvals", "01-plan-and-publish-approval") as (page, folder):
        load_folder(page, folder, "Summarise who owns order tracking and what the goal is.")
        page.check("#require-plan-approval")
        start(page)

        expect(page.locator("#approval-card")).to_contain_text("Read everything")
        expect(page.locator("#run-status")).to_have_class("run-status waiting_approval")
        page.fill("#approval-note", "Read only brief.md and name the owner.")
        hold(page)
        page.click("#deny-operation")  # "Request changes": the note goes back to the model

        approve(page, "Name the owner and the goal")
        approve(page, "Owner: Mei [brief.md E1]")  # the exact content, before anything is saved
        completed(page)
        expect(page.locator("#preview-name")).to_have_text("summary.md")
        expect(page.locator("#preview-text")).to_contain_text("Goal: one page that shows the order status.")
        expect(page.locator("#timeline .step.done").filter(has_text="E1")).to_have_count(1)
        expect(page.locator("#download")).to_have_attribute("href", re.compile(r"/api/runs/.+/artifacts/.+"))


def test_an_mcp_tool_with_side_effects_waits_for_approval_and_a_read_only_one_becomes_evidence(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "mcp", "02-mcp-tools-and-approval") as (page, folder):
        load_folder(page, folder, "Record Leo as the action owner in the notes, then report who owns the action.")
        page.click("details.advanced > summary")
        tools = page.locator("#mcp-tool-list input[type=checkbox]")
        expect(tools).to_have_count(2)  # only the tools the MCP config lists
        expect(page.locator("#mcp-tool-list")).to_contain_text("create_note")
        for tool in tools.all():
            tool.check()
        page.uncheck("#require-publish-approval")
        start(page)

        expect(page.locator("#run-status")).to_have_class("run-status waiting_approval")
        expect(page.locator("#timeline .step.done").filter(has_text="create_note")).to_have_count(0)  # not run yet
        approve(page, "Action owner: Leo")
        completed(page)
        expect(page.locator("#preview-name")).to_have_text("owner.md")
        expect(page.locator("#preview-text")).to_contain_text("Action owner: Leo [mcp/notes/search_notes E2]")
        expect(page.locator("#timeline .step.done").filter(has_text="E2")).to_have_count(1)


def test_a_skill_is_loaded_on_request_and_a_finished_run_drafts_a_new_skill(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "skill", "03-skills") as (page, folder):
        load_folder(page, folder, "Write the minutes of this meeting.")
        page.click("details.advanced > summary")
        expect(page.locator("#skill-list")).to_contain_text("Summarise meeting notes into decisions and owners.")
        page.check('#skill-list input[value="meeting-notes"]')
        page.uncheck("#require-publish-approval")
        start(page)

        completed(page)
        expect(page.locator("#timeline .step.done").filter(has_text="meeting-notes")).to_have_count(1)
        expect(page.locator("#preview-text")).to_contain_text("ship the tracker on Friday")
        hold(page)
        page.fill("#skill-name", "weekly-minutes")
        page.click("#draft-skill")
        expect(page.locator("#skill-draft-text")).to_contain_text("name: weekly-minutes")
        expect(page.locator("#skill-draft-path")).to_contain_text("weekly-minutes")
        expect(page.locator("#download-trace")).to_have_attribute("href", re.compile(r"/api/runs/.+/trace"))
        page.locator("#skill-draft-card").scroll_into_view_if_needed()


def test_a_command_hook_refuses_one_read_and_the_run_carries_on_with_the_rest(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "hook", "04-hook-denies-a-tool-call") as (page, folder):
        load_folder(page, folder, "Report the revenue.")
        page.uncheck("#require-publish-approval")
        start(page)

        completed(page)
        refused = page.locator("#timeline .step.failure")
        expect(refused).to_have_count(1)
        expect(refused).to_contain_text("secret.md")
        expect(page.locator("#timeline .step.done").filter(has_text="facts.md")).to_have_count(1)
        expect(page.locator("#preview-text")).to_contain_text("Revenue was 42. [facts.md E1]")
        hold(page)
        page.check("#show-all-events")  # the full saved log has the hook's own reason
        expect(page.locator("#timeline")).to_contain_text("secret.md is off limits for reports.")


def test_a_subagent_and_a_source_search_answer_a_question_without_writing_a_file(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "delegate", "05-subagent-search-and-direct-answer") as (page, folder):
        load_folder(page, folder, "What was the revenue, and how long was checkout down?")
        page.check("#subagents")
        page.check("#source-search")
        page.click("details.advanced > summary")
        page.select_option("#completion-workflow", "answer")
        start(page)

        completed(page)
        steps = page.locator("#timeline .substeps li")
        expect(steps.first).to_be_visible()  # the child's steps sit under the call that started it
        assert steps.count() >= 2
        expect(page.locator("#timeline .step.done").filter(has_text="checkout down minutes")).to_have_count(1)
        expect(page.locator("#run-result")).to_contain_text("Checkout was down for 47 minutes")
        expect(page.locator("#timeline")).to_contain_text("The answer's citations passed the checks")
        expect(page.locator("#preview-empty")).to_be_visible()  # an answer, not an artifact


def test_the_shell_switches_panels_keeps_widths_and_lists_what_waits_for_you(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "fixture", "06-app-shell-routines-and-attention") as (page, _):
        expect(page.locator("#tab-run")).to_be_disabled()
        page.click('[data-side="history"]')
        expect(page.locator('[data-side-section="history"]')).to_be_visible()
        expect(page.locator('[data-side-section="workspace"]')).to_be_hidden()
        page.click('[data-side="workspace"]')

        before = page.locator(".sidebar").bounding_box()["width"]
        page.focus("#split-side")
        for _ in range(3):
            page.keyboard.press("ArrowRight")
        assert page.locator(".sidebar").bounding_box()["width"] == before + 48
        page.reload()
        expect(page.locator("#split-side")).to_have_attribute("aria-valuenow", str(round(before + 48)))  # remembered

        page.click("#use-demo")
        expect(page.locator("#start-task")).to_be_enabled()
        start(page)
        expect(page.locator("#attention")).to_be_visible()  # the one list of Runs waiting for a decision
        expect(page.locator("#attention-list li")).to_have_count(1)
        approve(page, "source-snapshot.md")
        completed(page)
        expect(page.locator("#attention")).to_be_hidden()

        page.click("#save-routine")
        expect(page.locator('[data-side="routines"]')).to_have_attribute("aria-pressed", "true")  # the panel comes forward
        expect(page.locator("#routine-list li.routine")).to_have_count(1)
        hold(page)
        page.locator("#routine-list li.routine button").first.click()  # "Run now"
        expect(page.locator("#routine-feedback")).not_to_have_text("")

        page.click("#new-task")
        expect(page.locator("#tab-compose")).to_have_attribute("aria-selected", "true")
        page.select_option("#lang-select", "zh-TW")
        expect(page.locator("#tab-compose")).to_have_text("設定任務")
