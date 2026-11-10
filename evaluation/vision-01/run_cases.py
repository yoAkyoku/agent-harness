"""Run vision-01 once against a real local model and save every event (no reruns for a better score).

    uv run python evaluation/vision-01/run_cases.py OUTPUT_DIR

Starts a workbench (Qwen3.5 9B on Ollama, JSON tools, the same model as --vision-model), runs each
case through headless mode, writes OUTPUT_DIR/<case>.ndjson and OUTPUT_DIR/results.json, then stops the
workbench. Needs Ollama 0.40.10 with qwen3.5:9b-q4_K_M.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import httpx

HERE = Path(__file__).resolve().parent
PORT = 8791


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    suite = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "AGENT_HARNESS_TOKEN": token, "PYTHONUTF8": "1"}
    log = (out / "workbench.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "agent_harness", "--model", suite["model"], "--decision-protocol", "json_tools",
         "--store", str(out / "store"), "--port", str(PORT), "--vision-model", suite["vision_model"]],
        env=env, stdout=log, stderr=subprocess.STDOUT)
    results = []
    try:
        base = f"http://127.0.0.1:{PORT}"
        deadline = time.monotonic() + 300
        while True:
            if server.poll() is not None:
                raise SystemExit(f"workbench exited with {server.returncode}; see {out / 'workbench.log'}")
            try:
                if httpx.get(f"{base}/api/config", headers={"Authorization": f"Bearer {token}"}, timeout=5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit("workbench did not start")
            time.sleep(1)
        limits = suite["limits"]
        for case in suite["cases"]:
            started = time.monotonic()
            command = [sys.executable, "-m", "agent_harness.headless", "--url", base,
                       "--workspace", str(HERE / "workspace"), "--task", case["task"],
                       "--max-steps", str(limits["max_steps"]), "--max-tool-calls", str(limits["max_tool_calls"]),
                       *[part for source in case["sources"] for part in ("--source", source)], *case["flags"]]
            done = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=1800)
            (out / f"{case['id']}.ndjson").write_text(done.stdout, encoding="utf-8")
            lines = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
            final = lines[-1]["run"] if lines and lines[-1].get("kind") == "headless.result" else {}
            results.append({"id": case["id"], "exit_code": done.returncode, "seconds": round(time.monotonic() - started),
                            "status": final.get("status"), "result": final.get("result"), "error": final.get("error"),
                            "artifacts": [item["name"] for item in final.get("artifacts", [])],
                            "needs_approval": final.get("needs_approval"),
                            "tools": [event["data"]["tool"] for event in lines if event.get("kind") == "tool.completed"],
                            "transcriptions": [event["data"]["result"].get("transcription") for event in lines
                                               if event.get("kind") == "tool.completed" and event["data"]["tool"] == "source_read"],
                            "rejected": [f"{event['data']['tool']}:{event['data']['code']}" for event in lines
                                         if event.get("kind") in {"tool.rejected", "tool.failed"}],
                            "stderr": done.stderr[-600:]})
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    finally:
        server.terminate()
        try:
            server.wait(30)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
