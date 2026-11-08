"""Opt-in real-model case with an explicitly labelled, pre-read controlled failure."""

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess
import time

import httpx

from agent_harness_demo.implementation import source_identity
from validate_local_model import save, workbench_headers

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8789, help="Port of the explicitly started loopback controlled-fault workbench.")
    parser.add_argument("--decision-protocol", choices=("native", "json_tools"), default="native")
    parser.add_argument("--publication-workflow", choices=("direct", "draft_review"), default="direct")
    parser.add_argument("--completion-workflow", choices=("model_summary", "artifact_receipt"), default="model_summary")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Choose a valid loopback port.")
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    sources = directory / "sources"
    sources.mkdir()
    (sources / "handoff.md").write_bytes(b"# Release handoff\nOwner: Mei\nRelease gate: passing regression tests\nIncident escalation contact: Bo\n")
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=15, headers=workbench_headers(),
                      limits=httpx.Limits(max_keepalive_connections=0)) as client:
        config = client.get("/api/config").raise_for_status().json()
        assert config.get("implementation") == source_identity(ROOT / "src" / "agent_harness_demo"), "Service implementation differs or is unidentified; restart this checkout before admitting a validation Run."
        assert config["model"]["name"] == "qwen3.5:9b-q4_K_M" and config["model"]["transport_fixture"] is False
        assert config["model"].get("decision_protocol", "native") == args.decision_protocol, "Controlled service protocol does not match the explicit selection."
        assert config["validation"] == {"fault_once_source_read": True}
        save(directory / "manifest.json", {"started_at": datetime.now(timezone.utc).isoformat(), "config": config,
             "completion_workflow": args.completion_workflow,
             "validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
             "git_status": subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True),
             "policy": "one real model Run; first source_read deliberately fails before reading; no executor retry or fallback"})
        workspace = client.post("/api/workspaces", json={"path": str(sources)}).raise_for_status().json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"], "title": "受控讀取故障修正"}).raise_for_status().json()
        request = {"task": "請整理 handoff.md 的負責人、發布條件與事件升級窗口，附上來源，保存為 Markdown。", "sources": ["handoff.md"]}
        if args.publication_workflow != "direct":
            request["publication_workflow"] = args.publication_workflow
        if args.completion_workflow != "model_summary":
            request["completion_workflow"] = args.completion_workflow
        save(directory / "request.json", request)
        run = client.post(f"/api/sessions/{session['id']}/runs", json=request).raise_for_status().json()
        save(directory / "admission.json", run)
        print(f"Controlled fault case admitted: {run['id']}", flush=True)
        start = time.monotonic()
        while run["status"] in {"queued", "running"}:
            if time.monotonic() - start > 195:
                raise RuntimeError("Observation deadline exceeded; reconcile the saved Run ID without resubmitting.")
            time.sleep(1)
            run = client.get(f"/api/runs/{run['id']}").raise_for_status().json()
        events = client.get(f"/api/runs/{run['id']}/events").raise_for_status().json()["events"]
        save(directory / "run.json", run)
        save(directory / "events.json", events)
        with httpx.Client(trust_env=False, timeout=10) as model:
            save(directory / "loaded-model.json", model.get("http://127.0.0.1:11434/api/ps").raise_for_status().json())
        texts = []
        exports = []
        for artifact in run["artifacts"]:
            raw = client.get(artifact["download_url"]).raise_for_status().content
            assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
            artifact_directory = directory / "artifacts" / artifact["id"]
            artifact_directory.mkdir(parents=True, exist_ok=True)
            path = artifact_directory / artifact["name"]
            if path.exists():
                assert path.read_bytes() == raw, "Previously exported artifact changed; retain the original evidence."
            else:
                path.write_bytes(raw)
            exports.append({"id": artifact["id"], "name": artifact["name"], "sha256": artifact["sha256"],
                            "bytes": len(raw), "export_path": path.relative_to(directory).as_posix()})
            texts.append(raw.decode("utf-8").split("\n\n## Verified tool evidence", 1)[0])
        save(directory / "artifact-manifest.json", exports)
        faults = [event for event in events if event["kind"] == "tool.fault_injected"]
        corrections = [event for event in events if event["kind"] == "correction.available"]
        observed_corrections = [event for event in events if event["kind"] == "correction.observed"]
        successful_reads = [event for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read"]
        resolved_reads = [read for event in observed_corrections for read in successful_reads
                          if faults and faults[0]["data"]["call_id"] in event["data"].get("failed_call_ids", [])
                          and event["data"].get("tool") == "source_read"
                          and read["data"]["call_id"] == event["data"].get("successful_call_id")
                          and faults[0]["sequence"] < read["sequence"] < event["sequence"]]
        requests = [event for event in events if event["kind"] == "tool.requested"
                    and event["data"].get("tool") == "source_read"]
        same_source_retry = any(
            failed["data"].get("arguments", {}).get("path") == "handoff.md"
            and recovered["data"].get("arguments", {}).get("path") == "handoff.md"
            and failed["sequence"] < faults[0]["sequence"] < recovered["sequence"] < read["sequence"]
            and failed["data"]["call_id"] != recovered["data"]["call_id"]
            and any(correction["data"].get("call_id") == failed["data"]["call_id"]
                    and any(event["kind"] == "model.requested"
                            and correction["sequence"] < event["sequence"] < recovered["sequence"]
                            for event in events)
                    for correction in corrections)
            for read in resolved_reads for failed in requests for recovered in requests
            if failed["data"].get("call_id") == faults[0]["data"]["call_id"]
            and recovered["data"].get("call_id") == read["data"]["call_id"]
        ) if faults else False
        body = "\n".join(texts)
        checks = {"completed": run["status"] == "completed", "visible_plan": any(event["kind"] == "plan.updated" for event in events),
                  "one_labelled_fault": len(faults) == 1, "error_returned": len(corrections) == 1,
                  "correction_observed": bool(resolved_reads),
                  "same_source_retry": same_source_retry,
                  "read_recovered": bool(successful_reads), "body_owners": "Mei" in body and "Bo" in body,
                  "body_gate": bool(re.search(r"回歸|regression", body, re.I)), "artifact": bool(texts)}
        if faults and successful_reads and corrections:
            decision_reads = [read for read in resolved_reads if any(
                correction["data"]["call_id"] == faults[0]["data"]["call_id"]
                and any(event["kind"] == "model.requested"
                        and correction["sequence"] < event["sequence"] < read["sequence"] for event in events)
                for correction in corrections)]
            checks["new_model_decision_before_retry"] = bool(decision_reads)
            checks["new_call_identity"] = any(faults[0]["data"]["call_id"] != read["data"]["call_id"] for read in decision_reads)
        save(directory / "assessment.json", {"checks": checks, "manual_review_required": True, "quality_accepted": False,
             "run_elapsed_seconds": round((datetime.fromisoformat(events[-1]["timestamp"]) - datetime.fromisoformat(run["created_at"])).total_seconds(), 2)})
        print(f"{run['status']}: {checks}", flush=True)
        if not all(checks.values()):
            raise SystemExit("Controlled correction case failed; evidence retained, no retry.")


if __name__ == "__main__":
    main()
