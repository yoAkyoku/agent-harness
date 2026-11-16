"""Pushing a finished Run's trace to an OpenTelemetry collector (`--otlp-endpoint`).

Off by default. When an endpoint is set, the workbench POSTs each Run's OTLP/JSON trace there once the
Run has reached a final state. The endpoint must be https, or plain http on this computer. A collector
that is down or refuses the trace never changes the Run: the failure is only counted. The trace is the
same one `GET /api/runs/{id}/trace` returns, so it carries no prompt, source or result text.
"""
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.web import create_app
from test_workbench import finish


class Collector:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={})


def _run(client: TestClient, tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": "Save a source snapshot.", "sources": ["facts.md"]}).json()
    final, _ = finish(client, run)
    return final


def _wait(client: TestClient, key: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get("/api/config").json()["trace_export"]
        if status[key]:
            return status
        time.sleep(0.05)
    raise AssertionError(f"no {key} trace within 10 seconds")


def test_a_finished_run_is_pushed_once_as_otlp_json(tmp_path: Path) -> None:
    collector = Collector()
    app = create_app(tmp_path / "store", otlp_endpoint="http://127.0.0.1:4318/v1/traces", otlp_transport=httpx.MockTransport(collector))
    with TestClient(app) as client:
        final = _run(client, tmp_path)
        status = _wait(client, "exported")
        pulled = client.get(f"/api/runs/{final['id']}/trace").json()

    assert final["status"] == "completed"
    assert status == {"enabled": True, "endpoint": "http://127.0.0.1:4318/v1/traces", "exported": 1, "failed": 0}
    assert len(collector.requests) == 1
    request = collector.requests[0]
    assert request.method == "POST" and str(request.url) == "http://127.0.0.1:4318/v1/traces"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == pulled  # the same trace the pull endpoint returns
    assert "Revenue" not in request.content.decode()


def test_a_collector_failure_is_counted_and_never_changes_the_run(tmp_path: Path) -> None:
    app = create_app(tmp_path / "store", otlp_endpoint="https://collector.example.com/v1/traces",
                     otlp_transport=httpx.MockTransport(Collector(status=503)))
    with TestClient(app) as client:
        final = _run(client, tmp_path)
        status = _wait(client, "failed")
        assert client.get(f"/api/runs/{final['id']}").json()["status"] == "completed"
    assert (status["exported"], status["failed"]) == (0, 1)


def test_export_is_off_by_default_and_the_endpoint_is_checked(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        assert client.get("/api/config").json()["trace_export"] == {"enabled": False}
    for endpoint in ("http://collector.example.com/v1/traces", "https://user:pw@collector.example.com/v1/traces", "ftp://x/y"):
        with pytest.raises(ValueError):
            create_app(tmp_path / "other", otlp_endpoint=endpoint)
