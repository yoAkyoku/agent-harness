"""The workbench driven in a real browser, one recorded flow per theme.

Each test starts the workbench as a child process (tests/e2e_server.py) with a scripted model, opens it in
Chromium through Playwright, does what a person would do, and checks what the page shows. Every flow is
recorded; the videos are written to E2E_VIDEO_DIR (default .tmp/e2e-videos), a failed flow's as <name>.failed.webm.

Playwright is not a default dependency: `uv sync --group e2e`, then `uv run playwright install chromium`
(or set E2E_BROWSER_CHANNEL=msedge / chrome to use an installed browser). Without it these tests are
skipped, unless REQUIRE_BROWSER_TESTS is set, as it is in CI.

E2E_SLOW_MO (ms between browser actions), E2E_HOLD_MS (pause on each state worth seeing) and
E2E_REPLY_DELAY (seconds before each scripted model reply) slow a run down so the videos can be followed.
"""
from collections.abc import Iterator
from contextlib import contextmanager
import json
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


class Server:
    """The workbench child process for one scenario; it can be killed and started again on the same port and store."""

    def __init__(self, root: Path) -> None:
        self.root, self.process = root, None
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]

    def start(self, scenario: str) -> None:
        log = self.root / f"server-{scenario}.log"
        with log.open("wb") as output:
            self.process = subprocess.Popen([sys.executable, str(SERVER), scenario, str(self.root), str(self.port)], stdout=output,
                                            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 60
        while True:
            assert self.process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-4000:]
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/api/config", trust_env=False, timeout=5).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "The workbench did not start: " + log.read_text(encoding="utf-8", errors="replace")[-4000:]
            time.sleep(.1)

    def kill(self) -> None:
        """A crash: no shutdown, no chance to save anything more."""
        assert self.process is not None
        self.process.kill()
        self.process.wait(timeout=20)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=20)

    def requests(self) -> list[dict]:
        """What the scripted model received, in order."""
        return json.loads((self.root / "requests.json").read_text(encoding="utf-8"))


class Bench:
    """What a flow works with; unpacks as (page, workspace folder)."""

    def __init__(self, page: Page, folder: Path, server: Server) -> None:
        self.page, self.folder, self.server = page, folder, server

    def __iter__(self) -> Iterator:
        return iter((self.page, self.folder))


@contextmanager
def workbench(browser: playwright_api.Browser, tmp_path: Path, scenario: str, video: str, *, locale: str = "en-US",
              size: dict[str, int] = SIZE, scan: bool = False) -> Iterator[Bench]:
    """A running workbench for the scenario and a recorded page open on it."""
    server = Server(tmp_path)
    context = None
    passed = False
    try:
        server.start(scenario)
        # The page's CSP allows only its own scripts; the accessibility scan injects axe-core, so it needs the bypass.
        context = browser.new_context(viewport=size, locale=locale, record_video_dir=str(tmp_path / "video"), record_video_size=size,
                                      bypass_csp=scan)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/")
        expect(page.locator("#mode-badge")).not_to_have_text("")
        yield Bench(page, tmp_path / "workspace", server)
        hold(page)
        passed = True
    finally:
        if context is not None:
            recorded = context.pages[0].video if context.pages else None
            context.close()  # the video file is complete only after the context closes
            if recorded is not None:
                VIDEOS.mkdir(parents=True, exist_ok=True)
                shutil.move(recorded.path(), VIDEOS / f"{video}.webm" if passed else VIDEOS / f"{video}.failed.webm")
        server.stop()


def axe_violations(page: Page) -> list[str]:
    """Every axe-core (WCAG 2.x A and AA, plus best practices) violation on the page as it is now."""
    import importlib.util
    script = Path(importlib.util.find_spec("axe_playwright_python").origin).parent / "axe.min.js"  # type: ignore[arg-type]
    page.evaluate(script.read_text(encoding="utf-8"))  # read as UTF-8: the package's own loader breaks on a non-UTF-8 locale
    found = page.evaluate("""async () => (await axe.run(document, {resultTypes: ["violations"]})).violations
        .flatMap(v => v.nodes.map(n => `${v.id} (${v.impact}): ${n.target.join(" ")}`))""")
    return list(found)


def untranslated(page: Page) -> list[str]:
    """Visible interface text in Chinese while the page is in English; user and model content is left out."""
    return list(page.evaluate("""() => {
        const own = "#sources, #task-input, #preview-content, #plan-steps, #approval-content, #approval-parameters, #run-result, " +
                    "#workspace-path, #workspace-label, #workspace-breadcrumb, #sessions, #routine-list, #attention-list, option[lang]";
        const cjk = /[\u3400-\u9fff]/;
        const out = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode, element = node.parentElement;
            if (!cjk.test(node.textContent) || element.closest(own) || !element.checkVisibility()) continue;
            out.push(`${element.id || element.className || element.tagName}: ${node.textContent.trim().slice(0, 60)}`);
        }
        for (const element of document.querySelectorAll("[aria-label], [placeholder], [title]")) {
            if (element.closest(own) || !element.checkVisibility()) continue;
            for (const name of ["aria-label", "placeholder", "title"]) {
                const value = element.getAttribute(name);
                if (value && cjk.test(value)) out.push(`${element.id || element.tagName} [${name}]: ${value.slice(0, 60)}`);
            }
        }
        return out;
    }"""))


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
        page.click("details.advanced > summary")
        page.select_option("#time-limit", "300")
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
        runs = page.evaluate("""async () => {
            const sessions = await (await fetch("/api/sessions")).json();
            return (await fetch(`/api/sessions/${sessions[0].id}/runs`)).json();
        }""")
        assert runs[-1]["limits"]["timeout_seconds"] == 300  # the time limit chosen in Advanced options


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


def test_a_crashed_workbench_comes_back_and_the_run_resumes_from_what_was_confirmed(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "interrupted", "07-crash-and-resume") as bench:
        page = bench.page
        load_folder(page, bench.folder, "Save the revenue.")
        page.uncheck("#require-publish-approval")
        start(page)
        expect(page.locator("#timeline .step.done").filter(has_text="facts.md")).to_have_count(1)  # one confirmed read
        hold(page)

        bench.server.kill()  # the process dies while the model is working
        expect(page.locator("#connection-state")).to_contain_text("Disconnected")
        hold(page)
        bench.server.start("resumed")  # same store, same port; the startup check marks the Run interrupted
        if page.locator("#reload-run").is_visible():  # the page stops retrying after five attempts
            page.click("#reload-run")
        expect(page.locator("#run-status")).to_have_class("run-status interrupted")
        expect(page.locator("#recovery-card")).to_be_visible()
        expect(page.locator("#resume-run")).to_be_enabled()
        hold(page)

        page.click("#resume-run")
        completed(page)
        expect(page.locator("#preview-name")).to_have_text("revenue.md")
        page.check("#show-all-events")
        expect(page.locator("#timeline")).to_contain_text("Reused confirmed result")  # the read was not dispatched again
    dispatched = [request for request in bench.server.requests()]
    assert len(dispatched) == 3  # the resumed Run asked the model three times; the read came from the checkpoint


def test_an_instruction_added_while_running_reaches_the_model_and_the_run_can_be_cancelled(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "steer", "08-steer-and-cancel") as bench:
        page = bench.page
        load_folder(page, bench.folder, "Read the sources and save a snapshot.")
        start(page)
        expect(page.locator("#add-message")).to_be_visible()  # the model is still on its second step

        page.fill("#add-message-text", "Also list the weekly orders.")
        page.click("#add-message-send")
        (tmp_path / "go").touch()  # let the model answer
        expect(page.locator("#timeline .step.done").filter(has_text="facts.md")).to_have_count(1)
        expect(page.locator("#timeline")).to_contain_text("Also list the weekly orders.")
        hold(page)

        page.click("#cancel-run")  # the model is now silent; stop the Run
        expect(page.locator("#run-status")).to_have_class("run-status cancelled")
        expect(page.locator("#cancel-run")).to_be_hidden()
    last = bench.server.requests()[-1]
    assert "Also list the weekly orders." in json.dumps(last["messages"])  # it reached the model's next step


def test_errors_are_shown_where_they_happen_and_runs_fail_by_correction_limit_or_at_once_by_scope(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "errors", "09-errors-correction-limit-and-scope") as bench:
        page = bench.page
        page.fill("#workspace-path", str(tmp_path / "no-such-folder"))
        page.click("#workspace-form button[type=submit]")
        expect(page.locator("#error")).to_be_visible()  # a request the server refused, shown where it was made
        hold(page)

        load_folder(page, bench.folder, "Save the revenue.")
        page.uncheck("#require-publish-approval")
        start(page)
        expect(page.locator("#run-status")).to_have_class("run-status failed")
        refused = page.locator("#timeline .step.failure")
        expect(refused).to_have_count(3)  # an uncited claim, sent back twice, then the limit
        expect(refused.first).to_contain_text("revenue.md")
        expect(page.locator("#run-result")).to_contain_text("correction")
        expect(page.locator("#preview-empty")).to_be_visible()  # nothing was saved
        expect(page.locator("#save-routine")).to_be_disabled()  # only a completed Run can become a routine
        hold(page)

        page.click("#tab-compose")
        page.fill("#task-input", "Report the payroll total.")
        start(page)  # the model asks for a file that was never selected
        expect(page.locator("#run-status")).to_have_class("run-status failed")
        expect(page.locator("#timeline .step.failure")).to_have_count(1)  # outside the Run's scope: no second try
        expect(page.locator("#timeline .step.failure")).to_contain_text("payroll.md")


def test_every_state_of_a_run_passes_an_accessibility_scan_and_shows_no_untranslated_text(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "approvals", "10-accessibility-and-translation-scan", scan=True) as bench:
        page = bench.page
        problems: dict[str, list[str]] = {}

        def check(state: str) -> None:
            found = axe_violations(page) + untranslated(page)
            if found:
                problems[state] = found

        check("start")
        load_folder(page, bench.folder, "Summarise who owns order tracking and what the goal is.")
        page.check("#require-plan-approval")
        page.click("details.advanced > summary")
        check("sources and advanced options")
        start(page)
        expect(page.locator("#approval-card")).to_contain_text("Read everything")
        check("plan approval")
        page.click("#deny-operation")
        approve(page, "Name the owner and the goal")
        expect(page.locator("#approval-card")).to_contain_text("Owner: Mei")
        check("publish approval")
        page.click("#approve-operation")
        completed(page)
        check("completed")
        page.check("#show-all-events")
        check("every event")
        for side in ("routines", "memory", "history"):
            page.click(f'[data-side="{side}"]')
            check(f"{side} panel")
        page.focus("#split-side")
        check("splitter focused")
    assert not problems, problems


def test_the_chinese_interface_runs_a_task_end_to_end(browser: playwright_api.Browser, tmp_path: Path) -> None:
    with workbench(browser, tmp_path, "fixture", "11-chinese-interface", locale="zh-TW", scan=True) as bench:
        page = bench.page
        expect(page.locator("html")).to_have_attribute("lang", "zh-Hant")  # chosen from the browser's language
        expect(page.locator("#tab-compose")).to_have_text("設定任務")
        page.click("#use-demo")
        expect(page.locator("#selection-hint")).to_contain_text("3")
        start(page)
        expect(page.locator("#run-status")).to_have_text("等待你的確認")
        expect(page.locator("#approval-heading")).to_have_text("確認這次成果保存")
        assert axe_violations(page) == []
        approve(page, "source-snapshot.md")
        expect(page.locator("#run-status")).to_have_text("已完成")
        expect(page.locator("#timeline .step.done").first).to_contain_text("完成")
        expect(page.locator("#run-actions-heading")).to_have_text("這次執行之後")
        assert axe_violations(page) == []


def no_sideways_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


def test_below_1180_pixels_the_preview_moves_under_the_task(browser: playwright_api.Browser, tmp_path: Path) -> None:
    size = {"width": 1000, "height": 760}
    with workbench(browser, tmp_path, "fixture", "12-stacked-layout-1000px", size=size) as bench:
        page = bench.page
        page.click("#use-demo")
        start(page)
        approve(page, "source-snapshot.md")
        completed(page)
        main, preview = page.locator(".task-panel").bounding_box(), page.locator(".preview-panel").bounding_box()
        assert preview["y"] >= main["y"] + main["height"] - 1 and abs(preview["x"] - main["x"]) < 1  # under it, same column
        expect(page.locator("#preview-name")).to_be_visible()
        expect(page.locator("#split-inspect")).to_be_hidden()  # nothing to resize sideways
        expect(page.locator("#split-side")).to_be_visible()
        no_sideways_scroll(page)


def test_on_a_phone_width_the_page_scrolls_down_and_a_task_still_completes(browser: playwright_api.Browser, tmp_path: Path) -> None:
    size = {"width": 390, "height": 844}
    with workbench(browser, tmp_path, "fixture", "13-phone-width-390px", size=size) as bench:
        page = bench.page
        expect(page.locator("#split-side")).to_be_hidden()
        no_sideways_scroll(page)
        page.click("#use-demo")
        page.locator("#start-task").scroll_into_view_if_needed()
        start(page)
        approve(page, "source-snapshot.md")
        completed(page)
        page.locator("#preview-name").scroll_into_view_if_needed()
        expect(page.locator("#preview-name")).to_have_text("source-snapshot.md")
        no_sideways_scroll(page)
        assert page.evaluate("document.documentElement.scrollHeight > innerHeight")  # one column that scrolls down
