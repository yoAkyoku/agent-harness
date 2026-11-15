"""Run hard-01 once against a real local model, save every event and grade each case against cases.json.

    uv run python evaluation/hard-01/run_cases.py OUTPUT_DIR

Starts a workbench (Qwen3.5 9B on Ollama, JSON tools, subagents allowed), runs each case through headless
mode, writes OUTPUT_DIR/<case>.ndjson and OUTPUT_DIR/results.json, then stops the workbench. No reruns for
a better score: the grade is whatever the single run produced. Needs Ollama 0.40.10 with qwen3.5:9b-q4_K_M.

The grader only applies the checks written in cases.json before the run (`grade` below); it does not judge
wording. `python evaluation/hard-01/run_cases.py --grade OUTPUT_DIR` re-applies it to saved results.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
PORT = 8792
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def grade(truth: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Reasons this result does not meet the case's truth; an empty list is a pass."""
    problems: list[str] = []
    answer = result.get("result") or ""
    if result.get("status") != "completed":
        return [f"status {result.get('status')}: {(result.get('error') or {}).get('code')}"]
    for needle in truth.get("must_contain", []):
        if needle not in answer:
            problems.append(f"answer lacks {needle!r}")
    for needle in truth.get("must_not_contain", []):
        if needle in answer:
            problems.append(f"answer contains {needle!r}")
    if truth.get("must_contain_any") and not any(needle in answer for needle in truth["must_contain_any"]):
        problems.append("answer does not say the field is missing")
    if truth.get("must_not_match") and re.search(truth["must_not_match"], answer):
        problems.append("answer states a value for the missing field")
    if "must_contain_number" in truth:
        expected = float(truth["must_contain_number"])
        stated = [float(match.replace(",", "")) for match in _NUMBER.findall(answer)]
        if not any(abs(value - expected) <= 0.05 for value in stated):
            problems.append(f"no number within 0.05 of {expected}")
    if truth.get("cites") and truth["cites"] not in answer:
        problems.append(f"answer does not cite {truth['cites']}")
    if truth.get("expects_tool") and truth["expects_tool"] not in result.get("tools", []):
        problems.append(f"{truth['expects_tool']} was not used")
    if "artifacts" in truth and len(result.get("artifacts", [])) != truth["artifacts"]:
        problems.append(f"{len(result.get('artifacts', []))} artifacts, expected {truth['artifacts']}")
    for needle in truth.get("artifact_must_contain", []):
        if needle not in (result.get("artifact_text") or ""):
            problems.append(f"artifact lacks {needle!r}")
    return problems


def regrade(out: Path) -> int:
    suite = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    truths = {case["id"]: case["truth"] for case in suite["cases"]}
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    for result in results:
        result["problems"] = grade(truths[result["id"]], result)
        result["passed"] = not result["problems"]
        print(result["id"], "PASS" if result["passed"] else "FAIL", "; ".join(result["problems"]))
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    if sys.argv[1] == "--grade":
        return regrade(Path(sys.argv[2]).resolve())
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    suite = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "AGENT_HARNESS_TOKEN": token, "PYTHONUTF8": "1"}
    log = (out / "workbench.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "agent_harness", "--model", suite["model"], "--decision-protocol", "json_tools",
         "--store", str(out / "store"), "--port", str(PORT), *suite["workbench_args"]],
        env=env, stdout=log, stderr=subprocess.STDOUT)
    results = []
    try:
        base = f"http://127.0.0.1:{PORT}"
        headers = {"Authorization": f"Bearer {token}"}
        deadline = time.monotonic() + 300
        while True:
            if server.poll() is not None:
                raise SystemExit(f"workbench exited with {server.returncode}; see {out / 'workbench.log'}")
            try:
                if httpx.get(f"{base}/api/config", headers=headers, timeout=5).status_code == 200:
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
                       "--timeout-seconds", str(limits["timeout_seconds"]),
                       *[part for source in case["sources"] for part in ("--source", source)], *case["flags"]]
            done = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=1800)
            (out / f"{case['id']}.ndjson").write_text(done.stdout, encoding="utf-8")
            lines = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
            final = lines[-1]["run"] if lines and lines[-1].get("kind") == "headless.result" else {}
            artifact_text = None
            if final.get("artifacts"):
                artifact_text = httpx.get(final["artifacts"][0]["download_url"], headers=headers, timeout=30).text
            result = {"id": case["id"], "run_id": final.get("id"), "exit_code": done.returncode,
                      "seconds": round(time.monotonic() - started),
                      "status": final.get("status"), "result": final.get("result"), "error": final.get("error"),
                      "usage": final.get("usage"), "token_usage": final.get("token_usage"),
                      "artifacts": [item["name"] for item in final.get("artifacts", [])], "artifact_text": artifact_text,
                      "tools": [event["data"]["tool"] for event in lines if event.get("kind") == "tool.completed"],
                      "rejected": [f"{event['data']['tool']}:{event['data']['code']}" for event in lines
                                   if event.get("kind") in {"tool.rejected", "tool.failed"}],
                      "subagents": sum(event.get("kind") == "subagent.completed" for event in lines),
                      "compacted": [{key: event["data"][key] for key in ("evidence_id", "original_text_bytes", "summary_bytes", "chunks")}
                                    for event in lines if event.get("kind") == "context.compacted"],
                      "stderr": done.stderr[-600:]}
            result["problems"] = grade(case["truth"], result)
            result["passed"] = not result["problems"]
            results.append(result)
            print(json.dumps({key: result[key] for key in ("id", "passed", "problems", "seconds", "status")}, ensure_ascii=True), flush=True)
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
