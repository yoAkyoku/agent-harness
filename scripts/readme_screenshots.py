"""Retake the three screenshots at the top of the README.

    uv run python scripts/readme_screenshots.py [--channel msedge]

Starts the workbench with its built-in scripted demo model (no model, no API), loads the sample data, turns on
plan approval and takes docs/images/plan-approval.png, publish-approval.png and completed-run.png at 1440x900
in English. Needs the e2e dependency group and a browser (`uv run playwright install chromium`, or --channel).
"""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="An installed browser to use instead of Playwright's Chromium, e.g. msedge.")
    arguments = parser.parse_args()
    token = secrets.token_urlsafe(24)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as store:  # Windows may still hold the store lock
        server = subprocess.Popen([sys.executable, "-m", "agent_harness", "--store", store, "--port", str(port)], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "AGENT_HARNESS_TOKEN": token})
        try:
            deadline = time.monotonic() + 60
            while True:
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/api/config", headers={"Authorization": f"Bearer {token}"},
                                 trust_env=False, timeout=5).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline and server.poll() is None, "the workbench did not start"
                time.sleep(.2)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=arguments.channel)
                page = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US").new_page()
                page.goto(f"http://127.0.0.1:{port}/?token={token}")
                expect(page.locator("#mode-badge")).not_to_have_text("")
                page.click("#use-demo")
                expect(page.locator("#start-task")).to_be_enabled()
                page.check("#require-plan-approval")
                page.click("#start-task")
                expect(page.locator("#approval-heading")).to_have_text("Approve this plan")
                page.wait_for_timeout(600)
                page.screenshot(path=str(IMAGES / "plan-approval.png"))
                page.click("#approve-operation")
                expect(page.locator("#approval-heading")).to_have_text("Approve saving this artifact")
                page.wait_for_timeout(600)
                page.screenshot(path=str(IMAGES / "publish-approval.png"))
                page.click("#approve-operation")
                expect(page.locator("#run-status")).to_have_class("run-status completed")
                expect(page.locator("#preview-content")).to_be_visible()
                page.wait_for_timeout(800)
                page.screenshot(path=str(IMAGES / "completed-run.png"))
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=20)
    for name in ("plan-approval", "publish-approval", "completed-run"):
        print(f"docs/images/{name}.png  {(IMAGES / f'{name}.png').stat().st_size / 1e3:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
