"""Run an evaluation suite against a real local model, several times, and grade every run.

    uv run python evaluation/run_suite.py evaluation/hard-02 OUTPUT_DIR --repeat 3

Starts a workbench (the suite's model on Ollama, JSON tools, plus the suite's `workbench_args`), runs
every case `--repeat` times through headless mode, writes OUTPUT_DIR/<case>-<attempt>.ndjson,
OUTPUT_DIR/results.json (one entry per run) and OUTPUT_DIR/summary.json (passes per case), then stops
the workbench. Every run is kept and counted: there is no retry and no best-of selection.

The grader applies only the checks written in the suite's cases.json before the run; it does not judge
wording. `--grade OUTPUT_DIR` re-applies it to saved results.
"""
from __future__ import annotations

import argparse
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

PORT = 8793
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def grade(truth: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Reasons this run does not meet the case's truth; an empty list is a pass."""
    if result.get("status") != "completed":
        return [f"status {result.get('status')}: {(result.get('error') or {}).get('code')}"]
    problems: list[str] = []
    answer = result.get("result") or ""
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
    if truth.get("expects_compaction") and not result.get("compacted"):
        problems.append("the source was not compacted")
    if "artifacts" in truth and len(result.get("artifacts", [])) != truth["artifacts"]:
        problems.append(f"{len(result.get('artifacts', []))} artifacts, expected {truth['artifacts']}")
    for needle in truth.get("artifact_must_contain", []):
        if needle not in (result.get("artifact_text") or ""):
            problems.append(f"artifact lacks {needle!r}")
    return problems


def summarise(suite: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    cases = []
    for case in suite["cases"]:
        runs = [result for result in results if result["id"] == case["id"]]
        entry: dict[str, Any] = {"id": case["id"], "runs": len(runs), "passed": sum(result["passed"] for result in runs),
                                 "distinct_answers": len({result.get("result") for result in runs}),
                                 "problems": sorted({problem for result in runs for problem in result["problems"]})}
        for name, expected in (case.get("observe") or {}).items():  # recorded, never graded
            entry[f"observed_{name}"] = sum(set(expected) <= set(result.get(name, [])) for result in runs)
        cases.append(entry)
    corrections = sum(len(result.get("rejected", [])) for result in results)
    return {"model": suite["model"], "runs": len(results), "passed": sum(result["passed"] for result in results),
            "rejected_calls": corrections, "cases": cases}


def regrade(suite: dict[str, Any], out: Path) -> None:
    truths = {case["id"]: case["truth"] for case in suite["cases"]}
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    for result in results:
        result["problems"] = grade(truths[result["id"]], result)
        result["passed"] = not result["problems"]
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summarise(suite, results), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    cli = argparse.ArgumentParser(description="Run an evaluation suite on a real local model and grade every run.")
    cli.add_argument("suite", type=Path, help="Folder with cases.json and workspace/.")
    cli.add_argument("output", type=Path)
    cli.add_argument("--repeat", type=int, default=1, help="Runs per case (default 1); every run is kept and counted.")
    cli.add_argument("--temperature", type=float, default=0,
                     help="Sampling temperature for the workbench's model (default 0, where repeats reproduce the same run).")
    cli.add_argument("--exact-tokenizer-model", type=Path,
                     help="The model's GGUF blob, to count context tokens exactly instead of by the byte bound.")
    cli.add_argument("--grade", action="store_true", help="Only re-apply the grader to the saved results in OUTPUT.")
    arguments = cli.parse_args()
    here, out = arguments.suite.resolve(), arguments.output.resolve()
    suite = json.loads((here / "cases.json").read_text(encoding="utf-8"))
    if arguments.grade:
        regrade(suite, out)
        return 0
    out.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "AGENT_HARNESS_TOKEN": token, "PYTHONUTF8": "1"}
    log = (out / "workbench.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "agent_harness", "--model", suite["model"], "--decision-protocol", "json_tools",
         "--store", str(out / "store"), "--port", str(PORT), *suite.get("workbench_args", []),
         *(["--temperature", str(arguments.temperature)] if arguments.temperature else []),
         *(["--exact-tokenizer-model", str(arguments.exact_tokenizer_model)] if arguments.exact_tokenizer_model else [])],
        env=env, stdout=log, stderr=subprocess.STDOUT)
    results: list[dict[str, Any]] = []
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
        for attempt in range(1, arguments.repeat + 1):
            for case in suite["cases"]:
                started = time.monotonic()
                command = [sys.executable, "-m", "agent_harness.headless", "--url", base,
                           "--workspace", str(here / "workspace"), "--task", case["task"],
                           "--max-steps", str(limits["max_steps"]), "--max-tool-calls", str(limits["max_tool_calls"]),
                           "--timeout-seconds", str(limits["timeout_seconds"]),
                           *[part for source in case["sources"] for part in ("--source", source)], *case["flags"]]
                done = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=1800)
                (out / f"{case['id']}-{attempt}.ndjson").write_text(done.stdout, encoding="utf-8")
                lines = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
                final = lines[-1]["run"] if lines and lines[-1].get("kind") == "headless.result" else {}
                artifact_text = None
                if final.get("artifacts"):
                    artifact_text = httpx.get(final["artifacts"][0]["download_url"], headers=headers, timeout=30).text
                completed = [event["data"] for event in lines if event.get("kind") == "tool.completed"]
                result = {"id": case["id"], "attempt": attempt, "run_id": final.get("id"), "exit_code": done.returncode,
                          "seconds": round(time.monotonic() - started),
                          "status": final.get("status"), "result": final.get("result"), "error": final.get("error"),
                          "usage": final.get("usage"), "token_usage": final.get("token_usage"),
                          "artifacts": [item["name"] for item in final.get("artifacts", [])], "artifact_text": artifact_text,
                          "tools": [data["tool"] for data in completed],
                          "reads": sorted({data["result"]["path"] for data in completed
                                           if data["tool"] == "source_read" and "path" in data.get("result", {})}),
                          "rejected": [f"{event['data']['tool']}:{event['data']['code']}" for event in lines
                                       if event.get("kind") in {"tool.rejected", "tool.failed"}],
                          "subagents": sum(event.get("kind") == "subagent.completed" for event in lines),
                          "compacted": [{key: event["data"][key] for key in ("evidence_id", "original_text_bytes", "summary_bytes", "chunks")}
                                        for event in lines if event.get("kind") == "context.compacted"],
                          "stderr": done.stderr[-600:]}
                result["problems"] = grade(case["truth"], result)
                result["passed"] = not result["problems"]
                results.append(result)
                print(json.dumps({key: result[key] for key in ("id", "attempt", "passed", "problems", "seconds", "status")},
                                 ensure_ascii=True), flush=True)
    finally:
        server.terminate()
        try:
            server.wait(30)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / "summary.json").write_text(json.dumps(summarise(suite, results), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
