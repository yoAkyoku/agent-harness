"""A sampling temperature for local models, and concrete citation forms in answer mode.

Local models run at temperature 0 by default, so a repeated case reproduces the same Run. `--temperature`
(local Ollama models only, 0 to 1.5) lets an evaluation measure how much results vary. The default
request is unchanged, byte for byte.

In answer mode the model is told the exact citation forms its evidence allows, such as `[facts.md E1]`,
not only the pattern: small models otherwise spend a correction on an answer without citations.
"""
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelAdapter, ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply


def _body(settings: ModelSettings) -> dict:
    adapter = ModelAdapter(httpx.AsyncClient(), settings, transport_fixture=True)
    return adapter.request_body([{"role": "user", "content": "x"}], [], 256)


def test_the_default_request_is_unchanged_and_a_temperature_is_sent_when_set() -> None:
    default = ModelSettings.local("contract-model", stream=False)
    body = _body(default)
    assert body["temperature"] == 0 and isinstance(body["temperature"], int)
    assert "temperature" not in ModelAdapter(httpx.AsyncClient(), default, transport_fixture=True).info

    warm = ModelSettings.local("contract-model", stream=False, temperature=0.7)
    assert _body(warm)["temperature"] == 0.7
    assert ModelAdapter(httpx.AsyncClient(), warm, transport_fixture=True).info["temperature"] == 0.7


@pytest.mark.parametrize("value", [-0.1, 1.6, True, "0.5"])
def test_out_of_range_temperatures_are_refused(value: object) -> None:
    with pytest.raises(ValueError, match="temperature"):
        ModelSettings.local("contract-model", stream=False, temperature=value)  # type: ignore[arg-type]


def test_temperature_applies_only_to_local_models(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="temperature"):
        ModelSettings(temperature=0.5)  # the fixture has no sampling
    for arguments in (["--temperature", "0.5"], ["--provider", "anthropic", "--temperature", "0.5"],
                      ["--model", "some-model", "--temperature", "2"]):
        done = subprocess.run([sys.executable, "-m", "agent_harness", "--store", str(tmp_path / "store"), *arguments],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and "--temperature" in done.stderr, arguments


def test_answer_mode_names_the_exact_citation_forms_available(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "orders.csv").write_bytes(b"region,amount\nnorth,40\nsouth,2\n")
    (source / "notes.md").write_bytes(b"Owner: Mei\n")
    peer = CompletionPeer([
        tool_reply("plan_update", {"steps": ["Sum", "Read", "Answer"]}, "plan"),
        tool_reply("table_aggregate", {"path": "orders.csv", "column": "amount", "operation": "sum"}, "sum"),
        tool_reply("source_read", {"path": "notes.md", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "The total is 42 [orders.csv E1] and Mei owns it [notes.md E2]."}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={
            "task": "Total and owner?", "sources": ["orders.csv", "notes.md"], "completion_workflow": "answer"}).json()
        final, _ = finish(client, run)

    assert final["status"] == "completed", final["error"]
    system = [request["messages"][0]["content"] for request in peer.requests]
    assert "Cite as:" not in system[0] and "Cite as:" not in system[1]  # nothing to cite yet
    assert system[2].endswith("Cite as: [orders.csv E1].")
    assert system[3].endswith("Cite as: [orders.csv E1] [notes.md E2].")
