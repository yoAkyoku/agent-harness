"""Opt-in, loopback-only acceptance. Never invoked by pytest; stops on first failure."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
import subprocess
from pathlib import Path
import time

import httpx


def save(path: Path, value: object) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Observe saved Run IDs; never resubmit admitted tasks or rerun completed cases.")
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=args.resume)
    if args.resume and not (directory / "manifest.json").is_file():
        parser.error("--resume requires an existing batch manifest")
    cases = [
        ("analysis", {"metrics.csv": "month,orders,exceptions\nJan,120,12\nFeb,150,9\nMar,180,9\n"},
         "請分析 metrics.csv，計算訂單總數、異常總數及整體異常率（異常數 / 訂單數 × 100），寫成有來源依據的 Markdown 摘要。", ["450", "30", "6.666667"]),
        ("documents", {"launch.md": "# Launch checklist\nOwner: Mei\nRelease requires passing regression tests.\n", "handoff.md": "# Support handoff\nOwner: Bo\nEscalate unresolved incidents to Mei.\n"},
         "請整理這兩份文件的索引：每份的主題、負責人與一項關鍵事項，附上來源，保存為 Markdown。", []),
        ("changed-analysis", {"metrics.csv": "month,orders,exceptions\nApr,80,4\nMay,120,6\n"},
         "請分析 metrics.csv，計算訂單總數、異常總數及整體異常率（異常數 / 訂單數 × 100），寫成有來源依據的 Markdown 摘要。", ["200", "10", "5.000000"]),
    ]
    with httpx.Client(base_url="http://127.0.0.1:8788", trust_env=False, timeout=15,
                      limits=httpx.Limits(max_keepalive_connections=0)) as client:
        config = client.get("/api/config").raise_for_status().json()
        assert config["model"]["name"] == "qwen3.5:9b-q4_K_M" and config["model"]["transport_fixture"] is False
        manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "config": config,
             "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
             "git_status": subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True),
             "limits": {"max_steps": 12, "max_tool_calls": 20, "timeout_seconds": 180}, "case_order": [case[0] for case in cases],
             "policy": "one attempt each, stop at first runtime or oracle failure; no fixture, fallback or model download"}
        if args.resume:
            original = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            assert original["config"]["model"] == config["model"], "Model configuration changed; do not resume this batch."
            assert original["git_revision"] == manifest["git_revision"], "Implementation revision changed; create a new batch."
            save(directory / ("observer-resume-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json"), manifest)
        else:
            save(directory / "manifest.json", manifest)
        for name, sources, task, expected in cases:
            case_directory = directory / name
            assessment_path = case_directory / "assessment.json"
            if args.resume and assessment_path.exists():
                assessed = json.loads(assessment_path.read_text(encoding="utf-8"))
                if not all(assessed["checks"].values()):
                    raise SystemExit("Previously failed case retained; do not rerun it through --resume.")
                print(f"{name}: existing completed assessment retained", flush=True)
                continue
            case_directory.mkdir(exist_ok=args.resume)
            source_directory = case_directory / "sources"
            source_directory.mkdir(exist_ok=args.resume)
            for filename, content in sources.items():
                source_path = source_directory / filename
                if not source_path.exists():
                    source_path.write_text(content, encoding="utf-8", newline="")
                assert source_path.read_bytes() == content.encode("utf-8"), "Original batch input changed."
            request = {"task": task, "sources": list(sources)}
            admission_path = case_directory / "admission.json"
            if args.resume and admission_path.exists():
                admission = json.loads(admission_path.read_text(encoding="utf-8"))
                assert admission["task"] == task and [source["path"] for source in admission["sources"]] == list(sources)
                run = client.get(f"/api/runs/{admission['id']}").raise_for_status().json()
                print(f"{name}: observing existing {run['id']}", flush=True)
            else:
                if args.resume and (case_directory / "request.json").exists():
                    raise SystemExit("Admission outcome is unknown: request exists without a saved Run ID. Reconcile through the workbench; do not resubmit.")
                workspace = client.post("/api/workspaces", json={"path": str(source_directory)}).raise_for_status().json()
                session = client.post("/api/sessions", json={"workspace_id": workspace["id"], "title": name}).raise_for_status().json()
                save(case_directory / "request.json", request)
                run = client.post(f"/api/sessions/{session['id']}/runs", json=request).raise_for_status().json()
                save(admission_path, run)
                print(f"{name}: admitted {run['id']}", flush=True)
            start = time.monotonic()
            while run["status"] in {"queued", "running"}:
                if time.monotonic() - start > 195:
                    raise RuntimeError("Workbench exceeded the declared deadline; inspect the saved admission.")
                time.sleep(1)
                run = client.get(f"/api/runs/{run['id']}").raise_for_status().json()
            events = client.get(f"/api/runs/{run['id']}/events").raise_for_status().json()["events"]
            save(case_directory / "run.json", run)
            save(case_directory / "events.json", events)
            with httpx.Client(trust_env=False, timeout=10) as model_client:
                save(case_directory / "loaded-model.json", model_client.get("http://127.0.0.1:11434/api/ps").raise_for_status().json())
            artifacts = []
            for artifact in run["artifacts"]:
                raw = client.get(artifact["download_url"]).raise_for_status().content
                assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
                (case_directory / artifact["name"]).write_bytes(raw)
                artifacts.append(raw.decode("utf-8"))
            results = [event["data"]["result"] for event in events if event["kind"] == "tool.completed"]
            tool_path = [event["data"]["tool"] for event in events if event["kind"] == "tool.completed"]
            text = "\n".join(artifacts)
            # Tool-generated appendices are provenance, never proof of the model-written answer.
            body = "\n".join(artifact.split("\n\n## Verified tool evidence", 1)[0] for artifact in artifacts)
            checks = {"completed": run["status"] == "completed", "visible_plan": any(event["kind"] == "plan.updated" for event in events),
                      "artifact": bool(artifacts), "verified_citations": "## Verified tool evidence" in text,
                      "source_hashes": all(source["sha256"] in text for source in run["sources"]),
                      "numeric_oracle": all(value in [result.get("value") for result in results] for value in expected)}
            if expected:
                checks["calculation_path"] = "table_aggregate" in tool_path and "number_calculate" in tool_path
                checks["orders_in_body"] = bool(re.search(r"訂單[^\n]{0,60}\b" + expected[0] + r"\b", body))
                checks["exceptions_in_body"] = bool(re.search(r"異常[^\n]{0,60}\b" + expected[1] + r"\b", body))
                rate = r"6\.6[67]\d*" if name == "analysis" else r"5(?:\.0+)?"
                checks["rate_in_body"] = bool(re.search(rate + r"\s*(?:\*\*)?\s*%", body))
            else:
                checks["document_answer"] = all(word in body for word in ("Mei", "Bo", "launch.md", "handoff.md"))
                checks["launch_topic_and_fact"] = bool(re.search(r"發布|發佈|release|launch", body, re.I)) and bool(re.search(r"回歸|regression", body, re.I))
                checks["handoff_topic_and_fact"] = bool(re.search(r"交接|handoff", body, re.I)) and bool(re.search(r"升級|轉交|escalat", body, re.I))
                checks["document_path"] = "source_read" in tool_path and "table_aggregate" not in tool_path
            save(case_directory / "assessment.json", {"checks": checks, "tool_path": tool_path, "elapsed_seconds": round(time.monotonic() - start, 2),
                 "run_elapsed_seconds": round((datetime.fromisoformat(events[-1]["timestamp"]) - datetime.fromisoformat(run["created_at"])).total_seconds(), 2),
                 "manual_review_required": True, "quality_accepted": False,
                 "manual_rubric": "Check each claim/number against the cited source and tool result; reject contradictions, swapped owners, missing themes/key facts and unsupported claims. Automated lexical checks are necessary only, never sufficient."})
            print(f"{name}: {run['status']} {checks}", flush=True)
            if not all(checks.values()):
                raise SystemExit("Stopped at first failed case; remaining cases were not run.")


if __name__ == "__main__":
    main()
