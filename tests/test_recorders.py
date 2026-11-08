"""Recorder HTTP wire fixtures are not real-model acceptance evidence."""

import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_harness_demo.web import create_app


def controlled_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, observed_failure: str | None = "failed", preplanned_read: bool = False, recovered_path: str | None = "handoff.md", split_evidence: bool = False) -> tuple[Path, str, dict[str, bytes]]:
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True, capture_output=True)
    (elsewhere / "marker").write_text("Different repository", encoding="utf-8")
    subprocess.run(["git", "add", "marker"], cwd=elsewhere, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Validation", "-c", "user.email=validation@example.invalid",
                    "commit", "-qm", "Different repository"], cwd=elsewhere, check=True, capture_output=True)
    with TestClient(create_app(tmp_path / "metadata")) as client:
        config = client.get("/api/config").json()
    # The recorder sees canned HTTP metadata; no model service is contacted.
    config["model"].update(name="qwen3.5:9b-q4_K_M", transport_fixture=False)
    config["validation"] = {"fault_once_source_read": True}
    raw = {"a" * 32: b"# First\nMei, Bo, passing regression tests.\n",
           "b" * 32: b"# Second\nMei, Bo, passing regression tests.\n"}
    identity = "c" * 32
    artifacts = [{"id": key, "name": "report.md", "sha256": hashlib.sha256(value).hexdigest(),
                  "download_url": f"/api/runs/{identity}/artifacts/{key}"} for key, value in raw.items()]
    run = {"id": identity, "status": "completed", "created_at": "2026-10-31T00:00:00+00:00", "artifacts": artifacts}
    events = [{"sequence": index, "kind": kind, "data": data, "timestamp": "2026-10-31T00:00:01+00:00"}
              for index, (kind, data) in enumerate([
                  ("plan.updated", {"steps": ["Read and publish"]}),
                  ("tool.requested", {"tool": "source_read", "call_id": "failed", "arguments": {"path": "handoff.md"}}),
                  ("tool.fault_injected", {"call_id": "failed"}),
                  ("correction.available", {"call_id": "failed"}),
                  *([("tool.requested", {"tool": "source_read", "call_id": "early", "arguments": {"path": "handoff.md"}}),
                     ("tool.completed", {"tool": "source_read", "call_id": "early"}),
                     ("correction.observed", {"failed_call_ids": ["failed"], "successful_call_id": "early", "tool": "source_read"})] if split_evidence else []),
                  *([("tool.completed", {"tool": "source_read", "call_id": "preplanned"})] if preplanned_read else []),
                  ("model.requested", {"step": 2}),
                  ("tool.requested", {"tool": "source_read", "call_id": "recovered", "arguments": {"path": recovered_path}}),
                  ("tool.completed", {"tool": "source_read", "call_id": "recovered"}),
                  *([("correction.observed", {"failed_call_ids": [observed_failure],
                      "successful_call_id": "recovered", "tool": "source_read"})] if observed_failure is not None else []),
                  ("run.completed", {}),
              ], start=1)]
    def transport(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/config":
            return httpx.Response(200, json=config)
        if path == "/api/workspaces":
            return httpx.Response(201, json={"id": "workspace"})
        if path == "/api/sessions":
            return httpx.Response(201, json={"id": "session"})
        if path == "/api/sessions/session/runs":
            return httpx.Response(202, json=run)
        if path == f"/api/runs/{identity}/events":
            return httpx.Response(200, json={"events": events})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        for key, value in raw.items():
            if path == f"/api/runs/{identity}/artifacts/{key}":
                return httpx.Response(200, content=value)
        raise AssertionError(f"Unexpected external request: {request.method} {request.url}")
    output = tmp_path / "recording"
    monkeypatch.chdir(elsewhere)
    monkeypatch.syspath_prepend(str(root / "scripts"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    monkeypatch.setattr(sys, "argv", [str(root / "scripts" / "validate_correction.py"), "--output", str(output)])
    runpy.run_path(str(root / "scripts" / "validate_correction.py"), run_name="__main__")
    return output, revision, raw


def test_controlled_recorder_uses_its_own_repository_when_invoked_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output, revision, _ = controlled_recording(tmp_path, monkeypatch)
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["git_revision"] == revision


@pytest.mark.parametrize("observed_failure", [None, "unrelated"])
def test_controlled_recorder_requires_correction_of_the_injected_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed_failure: str | None) -> None:
    with pytest.raises(SystemExit, match="failed"):
        controlled_recording(tmp_path, monkeypatch, observed_failure=observed_failure)
    assessment = json.loads((tmp_path / "recording/assessment.json").read_text(encoding="utf-8"))
    assert assessment["checks"]["read_recovered"] is True
    assert assessment["checks"]["correction_observed"] is False


def test_controlled_recorder_uses_the_read_named_by_the_correction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output, _, _ = controlled_recording(tmp_path, monkeypatch, preplanned_read=True)
    checks = json.loads((output / "assessment.json").read_text(encoding="utf-8"))["checks"]
    assert checks["correction_observed"] is True
    assert checks["new_model_decision_before_retry"] is True
    assert checks["new_call_identity"] is True


@pytest.mark.parametrize("recovered_path", ["other.md", None])
def test_controlled_recorder_rejects_recovery_of_a_different_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovered_path: str | None) -> None:
    with pytest.raises(SystemExit, match="failed"):
        controlled_recording(tmp_path, monkeypatch, recovered_path=recovered_path)
    checks = json.loads((tmp_path / "recording/assessment.json").read_text(encoding="utf-8"))["checks"]
    assert checks["same_source_retry"] is False


def test_controlled_recorder_does_not_combine_different_retry_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="failed"):
        controlled_recording(tmp_path, monkeypatch, recovered_path="other.md", split_evidence=True)
    checks = json.loads((tmp_path / "recording/assessment.json").read_text(encoding="utf-8"))["checks"]
    assert checks["same_source_retry"] is False


def test_controlled_recorder_preserves_same_named_artifact_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output, _, raw = controlled_recording(tmp_path, monkeypatch)
    for identity, content in raw.items():
        assert (output / "artifacts" / identity / "report.md").read_bytes() == content
    exports = json.loads((output / "artifact-manifest.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in exports} == set(raw)
    assert all((output / item["export_path"]).read_bytes() == raw[item["id"]] for item in exports)
    assert all(item["sha256"] == hashlib.sha256(raw[item["id"]]).hexdigest() for item in exports)


@pytest.mark.parametrize("workflow", ["direct", "draft_review"])
@pytest.mark.parametrize("completion", ["model_summary", "artifact_receipt"])
def test_quality_recorder_freezes_explicit_publication_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: str, completion: str) -> None:
    root = Path(__file__).resolve().parents[1]
    with TestClient(create_app(tmp_path / "metadata")) as client:
        config = client.get("/api/config").json()
    config["model"].update(name="qwen3.5:9b-q4_K_M", transport_fixture=False,
                           digest="56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90")
    config["validation"] = {}
    requests = []
    def transport(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json=config)
        if request.url.path == "/api/workspaces":
            return httpx.Response(201, json={"id": "workspace"})
        if request.url.path == "/api/sessions":
            return httpx.Response(201, json={"id": "session"})
        if request.url.path == "/api/sessions/session/runs":
            return httpx.Response(503, json={"detail": "Fixture stops at admission; no model was contacted."})
        raise AssertionError(str(request.url))
    original_output = subprocess.check_output
    def git_output(command, **kwargs):
        # Canned wire fixture intentionally exercises uncommitted recorder edits.
        if command[:3] == ["git", "status", "--porcelain"]:
            return ""
        return original_output(command, **kwargs)
    monkeypatch.setattr(subprocess, "check_output", git_output)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    monkeypatch.syspath_prepend(str(root / "scripts"))
    output = tmp_path / "batch"
    arguments = [str(root / "scripts/validate_demo.py"), "--output", str(output), "--case", "analysis-standard", "--publication-workflow", workflow,
                 "--completion-workflow", completion]
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(httpx.HTTPStatusError):
        runpy.run_path(arguments[0], run_name="__main__")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["publication_workflow"] == workflow
    assert manifest["completion_workflow"] == completion
    request = json.loads((output / "analysis-standard/request.json").read_text(encoding="utf-8"))
    assert request["publication_workflow"] == workflow
    assert request.get("completion_workflow", "model_summary") == completion
    admitted = sum(item.url.path == "/api/sessions/session/runs" for item in requests)
    arguments[-1] = "artifact_receipt" if completion == "model_summary" else "model_summary"
    with pytest.raises(AssertionError, match="completion workflow"):
        runpy.run_path(arguments[0], run_name="__main__")
    assert sum(item.url.path == "/api/sessions/session/runs" for item in requests) == admitted
    arguments[-1] = completion
    arguments[arguments.index("--publication-workflow") + 1] = "draft_review" if workflow == "direct" else "direct"
    with pytest.raises(AssertionError, match="workflow"):
        runpy.run_path(arguments[0], run_name="__main__")
    assert sum(item.url.path == "/api/sessions/session/runs" for item in requests) == admitted
