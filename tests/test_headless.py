"""Headless mode: submit a task to a running workbench, print its events as NDJSON, exit by outcome."""
import json
import os
from pathlib import Path
import subprocess
import sys

from test_recovery import running_service


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    return source


def _headless(url: str, *arguments: str, token: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if not token:
        environment.pop("AGENT_HARNESS_TOKEN", None)
    return subprocess.run([sys.executable, "-m", "agent_harness_demo.headless", "--url", url, *arguments],
                          capture_output=True, text=True, encoding="utf-8", env=environment, timeout=120)


def test_completed_run_streams_every_event_as_ndjson_and_exits_zero(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with running_service(tmp_path / "store") as service:
        result = _headless(str(service.client.base_url), "--workspace", str(source),
                           "--source", "facts.md", "--task", "Save a source snapshot.")

    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    events, summary = lines[:-1], lines[-1]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["kind"] == "run.completed"
    assert summary["kind"] == "headless.result"
    assert summary["run"]["status"] == "completed"
    assert [artifact["name"] for artifact in summary["run"]["artifacts"]] == ["source-snapshot.md"]
    assert summary["run"]["artifacts"][0]["download_url"].startswith(str(service.client.base_url))


def test_failed_run_exits_one_with_its_error(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with running_service(tmp_path / "store") as service:
        result = _headless(str(service.client.base_url), "--workspace", str(source),
                           "--source", "facts.md", "--task", "Save a source snapshot.", "--max-steps", "1")

    assert result.returncode == 1, result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["run"]["status"] == "failed"
    assert summary["run"]["error"]["code"] == "step_limit_exceeded"


def test_rejected_request_exits_two_without_events(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with running_service(tmp_path / "store") as service:
        missing = _headless(str(service.client.base_url), "--workspace", str(source),
                            "--source", "missing.md", "--task", "Save a source snapshot.")
        no_token = _headless(str(service.client.base_url), "--workspace", str(source),
                             "--source", "facts.md", "--task", "Save a source snapshot.", token=False)

    assert missing.returncode == 2 and missing.stdout == ""
    assert "missing.md" in missing.stderr or "source" in missing.stderr.lower()
    assert no_token.returncode == 2 and "AGENT_HARNESS_TOKEN" in no_token.stderr
