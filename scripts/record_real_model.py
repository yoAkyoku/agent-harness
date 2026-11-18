"""Record one task done by a real local model in the workbench, as a person would do it in the browser.

    uv run python scripts/record_real_model.py OUT_DIR [--model qwen3.5:9b-q4_K_M] [--channel msedge]

Ollama must already be serving the model. The workbench is started exactly as the README says for a local model
(json_tools decision protocol); the browser loads the sample data, gives the task below with publish approval on,
approves the artifact the model proposes (the approval card is part of the recording), and waits for the end.

This is an observation, not a test: a real model is not deterministic, and one recording says nothing about how
often it succeeds. Whatever happens is kept: OUT_DIR gets the video, the Run, its events, the artifact if any, and
outcome.json. The criteria are fixed here, before the run: the Run must end `completed` with one saved artifact
(which means the harness verified every citation). Whether the content is right is read and noted by a person.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TASK = "整理這個改版的目標、已知風險，以及三週訂單數的變化，存成一份附引用的摘要。"
SIZE = {"width": 1280, "height": 800}
LIMIT_SECONDS = 15 * 60  # how long the recorder waits; the Run's own limit is --time-limit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--model", default="qwen3.5:9b-q4_K_M")
    parser.add_argument("--channel", help="An installed browser to use instead of Playwright's Chromium, e.g. msedge.")
    parser.add_argument("--time-limit", choices=("180", "300", "600"), default="180",
                        help="The Run's time limit in seconds, chosen in Advanced options as a person would (default 180).")
    parser.add_argument("--temperature", type=float, default=0,
                        help="Sampling temperature for the workbench's model (default 0, where a repeat reproduces the same Run).")
    parser.add_argument("--exact-tokenizer-model", type=Path,
                        help="The model's GGUF blob, to count context tokens exactly instead of by the byte bound.")
    arguments = parser.parse_args()
    out = arguments.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(24)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    store = Path(tempfile.mkdtemp(prefix="real-model-store-"))
    log = (out / "server.log").open("wb")
    server = subprocess.Popen([sys.executable, "-m", "agent_harness", "--model", arguments.model, "--decision-protocol", "json_tools",
                               "--store", str(store), "--port", str(port),
                               *(["--temperature", str(arguments.temperature)] if arguments.temperature else []),
                               *(["--exact-tokenizer-model", str(arguments.exact_tokenizer_model)] if arguments.exact_tokenizer_model else [])], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                              env={**os.environ, "AGENT_HARNESS_TOKEN": token})
    api = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=30)
    outcome: dict = {"model": arguments.model, "task": TASK, "time_limit_seconds": int(arguments.time_limit), "temperature": arguments.temperature, "exact_tokens": arguments.exact_tokenizer_model is not None, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "criteria": "Run ends completed with one saved artifact (all citations verified by the harness)"}
    try:
        deadline = time.monotonic() + 120
        while True:
            assert server.poll() is None, (out / "server.log").read_text(encoding="utf-8", errors="replace")[-3000:]
            try:
                config = api.get("/api/config").json()
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "the workbench did not start"
                time.sleep(.3)
        outcome["model_config"] = config.get("model")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=arguments.channel)
            context = browser.new_context(viewport=SIZE, locale="en-US", record_video_dir=str(out / "video"), record_video_size=SIZE)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{port}/?token={token}")
            expect(page.locator("#mode-badge")).not_to_have_text("", timeout=30_000)
            page.click("#use-demo")
            expect(page.locator("#start-task")).to_be_enabled()
            page.fill("#task-input", TASK)
            if arguments.time_limit != "180":
                page.click("details.advanced > summary")
                page.select_option("#time-limit", arguments.time_limit)
            page.wait_for_timeout(1500)
            started = time.monotonic()
            page.click("#start-task")
            approvals = 0
            while time.monotonic() - started < LIMIT_SECONDS:
                status = page.get_attribute("#run-status", "class") or ""
                if any(final in status for final in ("completed", "failed", "cancelled", "interrupted")):
                    break
                if page.locator("#approval-card").is_visible() and page.locator("#approve-operation").is_enabled():
                    page.wait_for_timeout(3000)  # long enough to read the card in the recording
                    page.click("#approve-operation")
                    approvals += 1
                page.wait_for_timeout(1000)
            outcome["seconds"] = round(time.monotonic() - started)
            outcome["approvals_given"] = approvals
            page.wait_for_timeout(3000)
            if page.locator("#preview-content").is_visible():
                page.locator("#preview-text").scroll_into_view_if_needed()
                page.wait_for_timeout(3000)
            video = page.video
            context.close()
            browser.close()
            if video is not None:
                shutil.move(video.path(), out / "recording.webm")
        sessions = api.get("/api/sessions").json()
        runs = api.get(f"/api/sessions/{sessions[0]['id']}/runs").json() if sessions else []
        if runs:
            run = runs[-1]
            outcome.update(run_id=run["id"], status=run["status"], error=run.get("error"), artifacts=[a["name"] for a in run.get("artifacts", [])],
                           usage=run.get("usage"))
            (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            events = api.get(f"/api/runs/{run['id']}/events").json()["events"]
            (out / "events.ndjson").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
            for artifact in run.get("artifacts", []):
                (out / f"artifact-{artifact['name']}").write_bytes(api.get(artifact["download_url"]).content)
        outcome["passed"] = outcome.get("status") == "completed" and len(outcome.get("artifacts", [])) == 1
    finally:
        api.close()
        server.terminate()
        server.wait(timeout=30)
        log.close()
        shutil.rmtree(store, ignore_errors=True)
    (out / "outcome.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
