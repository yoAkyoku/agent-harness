"""Recovery acceptance uses real child processes and public HTTP, never a simulated restart."""

from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
import os
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest


@dataclass
class RunningService:
    client: httpx.Client
    process: subprocess.Popen
    log: Path


def launch(store: Path, *extra: str, package_root: Path | None = None) -> tuple[subprocess.Popen, Path, int]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    store.parent.mkdir(parents=True, exist_ok=True)
    log = store.parent / f"service-{uuid4().hex}.log"
    command = [sys.executable, "-m", "agent_harness_demo", "--store", str(store), "--port", str(port), *extra]
    environment = os.environ.copy()
    if package_root is not None:
        environment["PYTHONPATH"] = str(package_root / "src")
    with log.open("wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   env=environment, cwd=package_root,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process, log, port


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


@contextmanager
def running_service(store: Path, *extra: str, package_root: Path | None = None) -> Iterator[RunningService]:
    process, log, port = launch(store, *extra, package_root=package_root)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-4000:]
            try:
                ready = client.get("/api/config").status_code == 200
            except httpx.TransportError:
                ready = False
            if ready:
                yield RunningService(client, process, log)
                return
            time.sleep(.03)
        pytest.fail("Child service did not become ready: " + log.read_text(encoding="utf-8", errors="replace")[-4000:])
    finally:
        client.close()
        stop(process)


def wait_event(client: httpx.Client, identity: str, kind: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = client.get(f"/api/runs/{identity}/events").json()["events"]
        match = next((event for event in events if event["kind"] == kind), None)
        if match:
            return match
        time.sleep(.02)
    pytest.fail(f"Expected public event {kind} was not saved.")


def create_process_task(client: httpx.Client, source: Path, **overrides: object) -> dict:
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    response = client.post(f"/api/sessions/{session['id']}/runs", json={
        "task": "Save a source snapshot.", "sources": ["facts.md"], **overrides,
    })
    assert response.status_code == 202, response.text
    return response.json()


def test_store_has_one_live_writer_and_kernel_ownership_releases_on_process_death(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "source_read") as first:
        run = create_process_task(first.client, source)
        wait_event(first.client, run["id"], "validation.tool_held")
        second, second_log, _ = launch(store)
        try:
            try:
                code = second.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pytest.fail("A second service opened the Store while its writer was alive.")
            assert code != 0
            assert "ownership" in second_log.read_text(encoding="utf-8", errors="replace").lower()
            assert first.client.get(f"/api/runs/{run['id']}").json()["status"] == "running"
            assert not any(event["kind"] == "run.interrupted" for event in first.client.get(f"/api/runs/{run['id']}/events").json()["events"])
        finally:
            stop(second)
        stop(first.process)
        with running_service(store) as replacement:
            assert replacement.client.get("/api/config").status_code == 200

def test_killed_executor_is_interrupted_and_unacknowledged_dispatch_is_unknown(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "source_read") as first:
        run = create_process_task(first.client, source)
        held = wait_event(first.client, run["id"], "validation.tool_held")
        previous = first.client.get(f"/api/runs/{run['id']}").json()
        before = first.client.get(f"/api/runs/{run['id']}/events").json()["events"]
        dispatched = next(event["data"] for event in before if event["kind"] == "tool.dispatched" and event["data"]["tool"] == "source_read")
        assert dispatched["arguments"] == {"path": "facts.md"}
        assert dispatched["scope"]["effect"] == "read_only"
        assert dispatched["scope"]["sources"] == previous["sources"]
        assert len(dispatched["parameters_sha256"]) == 64
        stop(first.process)
    with running_service(store) as replacement:
        recovered = replacement.client.get(f"/api/runs/{run['id']}").json()
        assert recovered["status"] == "interrupted", recovered
        assert recovered["recovery"]["previous_status"] == "running"
        assert recovered["recovery"]["reason"] == "executor_lost"
        assert recovered["usage"] == previous["usage"]
        events = replacement.client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert events[:len(before)] == before
        unknown = [event for event in events if event["kind"] == "tool.unknown"]
        assert len(unknown) == 1
        assert unknown[0]["data"]["call_id"] == held["data"]["call_id"]
        assert unknown[0]["data"]["effect"] == "read_only"
        assert unknown[0]["data"]["reason"] == "executor_lost"
        assert not any(event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read" for event in events)
    with running_service(store) as another:
        assert another.client.get(f"/api/runs/{run['id']}").json() == recovered
        assert another.client.get(f"/api/runs/{run['id']}/events").json()["events"] == events

def test_restart_expires_old_approval_without_losing_reviewed_parameters(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store) as first:
        run = create_process_task(first.client, source, require_publish_approval=True)
        requested = wait_event(first.client, run["id"], "approval.requested")
        approval = requested["data"]
        stop(first.process)
    with running_service(store) as replacement:
        recovered = replacement.client.get(f"/api/runs/{run['id']}").json()
        assert recovered["status"] == "interrupted"
        assert recovered["recovery"]["previous_status"] == "waiting_approval"
        old = recovered["approvals"][0]
        assert old["status"] == "expired", old
        assert old["arguments"] == approval["arguments"]
        assert old["parameters_sha256"] == approval["parameters_sha256"]
        assert recovered["recovery"]["uncertain_attempts"] == []
        assert recovered["artifacts"] == []
        rejected = replacement.client.post(f"/api/runs/{run['id']}/approvals/{old['id']}", json={"decision": "approve", "parameters_sha256": old["parameters_sha256"]})
        assert rejected.status_code == 409
        events = replacement.client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert any(event["kind"] == "approval.expired" and event["data"]["reason"] == "service_restarted" for event in events)
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write" for event in events)

def test_published_artifact_keeps_call_identity_and_bytes_across_process_death(tmp_path: Path) -> None:
    import hashlib
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "artifact_write") as first:
        run = create_process_task(first.client, source)
        held = wait_event(first.client, run["id"], "validation.tool_held")
        assert held["data"]["phase"] == "after_publish_before_result"
        saved = first.client.get(f"/api/runs/{run['id']}").json()
        artifact = saved["artifacts"][0]
        assert artifact["call_id"] == held["data"]["call_id"]
        raw = first.client.get(artifact["download_url"]).content
        assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
        events = first.client.get(f"/api/runs/{run['id']}/events").json()["events"]
        intent = next(event["data"] for event in events if event["kind"] == "tool.dispatched" and event["data"]["tool"] == "artifact_write")
        assert intent["arguments"]["content"].encode("utf-8") == raw
        assert not any(event["kind"] == "tool.completed" and event["data"]["tool"] == "artifact_write" for event in events)
        stop(first.process)
    with running_service(store) as replacement:
        recovered = replacement.client.get(f"/api/runs/{run['id']}").json()
        assert recovered["status"] == "interrupted"
        assert recovered["artifacts"] == [artifact]
        assert replacement.client.get(artifact["download_url"]).content == raw
        events = replacement.client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert sum(event["kind"] == "artifact.published" for event in events) == 1
        reconciled = [event for event in events if event["kind"] == "tool.reconciled"]
        assert len(reconciled) == 1
        assert reconciled[0]["data"]["call_id"] == artifact["call_id"]
        assert reconciled[0]["data"]["result"] == artifact
        assert recovered["recovery"]["uncertain_attempts"] == []
        checkpoint = replacement.client.get(f"/api/runs/{run['id']}/checkpoint").json()
        assert [call["tool"] for call in checkpoint["confirmed"]] == ["workspace_list", "source_read", "artifact_write"]
        assert checkpoint["pending"] == [] and checkpoint["blockers"] == []

def test_restart_checkpoint_contains_only_paired_confirmed_tool_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "source_read") as first:
        run = create_process_task(first.client, source)
        wait_event(first.client, run["id"], "validation.tool_held")
        stop(first.process)
    with running_service(store) as replacement:
        response = replacement.client.get(f"/api/runs/{run['id']}/checkpoint")
        assert response.status_code == 200, response.text
        checkpoint = response.json()
        assert checkpoint["source_run_id"] == run["id"]
        assert checkpoint["task"] == run["task"]
        assert len(checkpoint["sha256"]) == 64
        assert [call["tool"] for call in checkpoint["confirmed"]] == ["workspace_list"]
        assert [(call["tool"], call["status"]) for call in checkpoint["pending"]] == [("source_read", "unknown")]
        calls = [call["id"] for message in checkpoint["messages"] for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in checkpoint["messages"] if message["role"] == "tool"]
        assert calls == results and len(calls) == 1
        assert checkpoint["artifacts"] == [] and checkpoint["blockers"] == []
        recovered = replacement.client.get(f"/api/runs/{run['id']}").json()
        assert recovered["recovery"]["checkpoint_sha256"] == checkpoint["sha256"]

def test_replay_includes_recovery_metadata_after_an_older_terminal_event(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from agent_harness_demo.web import create_app
    from test_workbench import create_task
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, hold_first_tool="source_read")) as first:
        run = create_task(first, source)
        wait_event(first, run["id"], "validation.tool_held")
    with TestClient(create_app(store)) as replacement:
        saved = replacement.get(f"/api/runs/{run['id']}/events").json()["events"]
        terminal = next(event for event in saved if event["kind"] == "run.interrupted")
        cursor = terminal["sequence"] - 8
        assert cursor >= 0 and saved[-1]["sequence"] > terminal["sequence"]
        received = []
        with replacement.websocket_connect(f"/api/runs/{run['id']}/stream?after={cursor}") as stream:
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    received.append(stream.receive_json())
            assert closed.value.code == 1000
        assert received == [event for event in saved if event["sequence"] > cursor]

@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_unverifiable_publication_remains_unknown_and_blocks_checkpoint(tmp_path: Path, damage: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "artifact_write") as first:
        run = create_process_task(first.client, source)
        wait_event(first.client, run["id"], "validation.tool_held")
        artifact = first.client.get(f"/api/runs/{run['id']}").json()["artifacts"][0]
        stop(first.process)
    # External filesystem damage is an input fault; assertions remain at public HTTP.
    path = store / "artifacts" / artifact["id"]
    if damage == "changed":
        path.write_bytes(b"Externally changed.\n")
    else:
        path.unlink()
    with running_service(store) as replacement:
        recovered = replacement.client.get(f"/api/runs/{run['id']}").json()
        assert recovered["status"] == "interrupted"
        assert recovered["recovery"]["reconciled_attempts"] == []
        assert recovered["recovery"]["uncertain_attempts"][0]["call_id"] == artifact["call_id"]
        assert replacement.client.get(artifact["download_url"]).status_code == 409
        checkpoint = replacement.client.get(f"/api/runs/{run['id']}/checkpoint").json()
        assert checkpoint["blockers"] == [{"call_id": artifact["call_id"], "code": "unknown_effect", "effect": "immutable_artifact"}]
        assert not any(call["tool"] == "artifact_write" for call in checkpoint["confirmed"])
        denied = replacement.client.post(f"/api/runs/{run['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-blocked-effect"})
        assert denied.status_code == 409 and denied.json()["detail"]["code"] == "checkpoint_blocked"
        assert len(replacement.client.get(f"/api/sessions/{run['session_id']}/runs").json()) == 1
        events = replacement.client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert not any(event["kind"] == "tool.reconciled" for event in events)
        assert sum(event["kind"] == "artifact.published" for event in events) == 1

def test_checkpoint_retains_calls_not_yet_reached_in_a_partial_model_batch(tmp_path: Path) -> None:
    import json
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    async def peer(request: httpx.Request) -> httpx.Response:
        calls = [("catalog", "workspace_list", {}), ("read", "source_read", {"path": "facts.md"}),
                 ("publish", "artifact_write", {"name": "snapshot.md", "content": "# Snapshot\nRevenue was 42.\n"})]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                           for identity, name, arguments in calls]}}]})
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(peer), hold_first_tool="source_read")) as first:
        run = create_task(first, source)
        wait_event(first, run["id"], "validation.tool_held")
    with TestClient(create_app(store)) as replacement:
        checkpoint = replacement.get(f"/api/runs/{run['id']}/checkpoint").json()
        assert [(call["call_id"], call["status"]) for call in checkpoint["pending"]] == [("read", "unknown"), ("publish", "not_dispatched")]
        assert [call["tool"] for call in checkpoint["confirmed"]] == ["workspace_list"]
        calls = [call["id"] for message in checkpoint["messages"] for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in checkpoint["messages"] if message["role"] == "tool"]
        assert calls == results and len(calls) == 1

def test_failed_store_initialization_releases_database_handle_before_ownership(tmp_path: Path) -> None:
    import sqlite3
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    store = tmp_path / "store"
    store.mkdir()
    database = store / "workspace.sqlite3"
    database.write_bytes(b"Invalid SQLite input for recovery validation.\n")
    with pytest.raises(sqlite3.DatabaseError) as failure:
        with TestClient(create_app(store)):
            pytest.fail("A malformed Store started successfully.")
    assert failure.value is not None  # Retain its traceback while repairing the input.
    database.replace(store / "invalid-store.saved")
    with TestClient(create_app(store)) as repaired:
        assert repaired.get("/api/config").status_code == 200

def test_explicit_resume_creates_new_run_and_continues_after_confirmed_catalog(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "source_read") as first:
        original = create_process_task(first.client, source)
        wait_event(first.client, original["id"], "validation.tool_held")
        stop(first.process)
    with running_service(store) as replacement:
        old = replacement.client.get(f"/api/runs/{original['id']}").json()
        old_events = replacement.client.get(f"/api/runs/{original['id']}/events").json()
        checkpoint = replacement.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        body = {"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-first-001"}
        response = replacement.client.post(f"/api/runs/{original['id']}/resume", json=body)
        assert response.status_code == 202, response.text
        resumed = response.json()
        assert resumed["id"] != old["id"] and resumed["session_id"] == old["session_id"]
        assert resumed["resume_checkpoint"]["source_run_id"] == old["id"]
        assert resumed["resume_checkpoint"]["sha256"] == checkpoint["sha256"]
        wait_event(replacement.client, resumed["id"], "run.completed")
        final = replacement.client.get(f"/api/runs/{resumed['id']}").json()
        assert final["status"] == "completed", final
        events = replacement.client.get(f"/api/runs/{resumed['id']}/events").json()["events"]
        dispatched = [event["data"]["tool"] for event in events if event["kind"] == "tool.dispatched"]
        assert dispatched == ["source_read", "artifact_write"]
        assert replacement.client.get(final["artifacts"][0]["download_url"]).content.endswith(b"Revenue was 42.\n")
        repeat = replacement.client.post(f"/api/runs/{old['id']}/resume", json=body)
        assert repeat.status_code == 202 and repeat.json() == final
        assert replacement.client.get(f"/api/runs/{old['id']}").json() == old
        assert replacement.client.get(f"/api/runs/{old['id']}/events").json() == old_events

def test_resume_restores_cited_evidence_but_requires_a_new_publish_approval(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from agent_harness_demo.model import ModelSettings
    from test_workbench import create_task, finish
    from test_local_model import LocalPeer
    from test_approval import pending_approval
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(LocalPeer()))) as first:
        run = create_task(first, source, require_publish_approval=True)
        old_approval = pending_approval(first, run)
    peer = LocalPeer()
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(peer))) as replacement:
        checkpoint = replacement.get(f"/api/runs/{run['id']}/checkpoint").json()
        response = replacement.post(f"/api/runs/{run['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-citations-001"})
        assert response.status_code == 202, response.text
        resumed = response.json()
        new_approval = pending_approval(replacement, resumed)
        assert new_approval["id"] != old_approval["id"]
        assert new_approval["parameters_sha256"] != old_approval["parameters_sha256"]
        assert new_approval["arguments"]["content"] == old_approval["arguments"]["content"]
        assert replacement.get(f"/api/runs/{resumed['id']}").json()["artifacts"] == []
        replacement.post(f"/api/runs/{resumed['id']}/approvals/{new_approval['id']}", json={"decision": "approve", "parameters_sha256": new_approval["parameters_sha256"]})
        final, events = finish(replacement, resumed)
        assert final["status"] == "completed", final["error"]
        assert [e["data"]["tool"] for e in events if e["kind"] == "tool.dispatched"] == ["artifact_write"]
        text = replacement.get(final["artifacts"][0]["download_url"]).text
        assert "[E1] facts.md:L1-L1" in text
        loaded = next(e["data"] for e in events if e["kind"] == "checkpoint.loaded")
        assert loaded["restored_evidence_ids"] == ["E1"]
        messages = peer.report.requests[0]["messages"]
        calls = [call["id"] for message in messages for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
        assert calls == results and len(calls) == 2

def test_resume_reuses_a_confirmed_tool_result_without_dispatching_it_again(tmp_path: Path) -> None:
    import json
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from agent_harness_demo.model import ModelSettings
    from test_workbench import create_task, finish, tool_reply
    from test_local_model import LocalPeer
    from test_approval import pending_approval
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(LocalPeer()))) as first:
        run = create_task(first, source, require_publish_approval=True)
        old_approval = pending_approval(first, run)
    peer = LocalPeer()
    requests = []
    async def retrying_model(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return tool_reply("source_read", {"path": "facts.md"}, "read-again")
        if len(requests) == 2:
            return tool_reply("artifact_write", {**old_approval["arguments"], "citations": ["E1"]}, "publish-new")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved."}}]})
    peer.report = retrying_model
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(peer))) as replacement:
        checkpoint = replacement.get(f"/api/runs/{run['id']}/checkpoint").json()
        expected = next(call["result"] for call in checkpoint["confirmed"] if call["tool"] == "source_read")
        response = replacement.post(f"/api/runs/{run['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-reuse-001"})
        assert response.status_code == 202, response.text
        resumed = response.json()
        approval = pending_approval(replacement, resumed)
        replacement.post(f"/api/runs/{resumed['id']}/approvals/{approval['id']}", json={"decision": "approve", "parameters_sha256": approval["parameters_sha256"]})
        final, events = finish(replacement, resumed)
        assert final["status"] == "completed", final["error"]
        assert [e["data"]["tool"] for e in events if e["kind"] == "tool.dispatched"] == ["artifact_write"]
        reused = [e["data"] for e in events if e["kind"] == "tool.reused"]
        assert len(reused) == 1 and reused[0]["result"] == expected
        assert reused[0]["source_run_id"] == run["id"]
        assert json.loads(requests[1]["messages"][-1]["content"]) == expected

def test_resume_after_reconciled_publish_references_original_artifact_without_republishing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "artifact_write") as first:
        original = create_process_task(first.client, source)
        wait_event(first.client, original["id"], "validation.tool_held")
        artifact = first.client.get(f"/api/runs/{original['id']}").json()["artifacts"][0]
        raw = first.client.get(artifact["download_url"]).content
        stop(first.process)
    with running_service(store) as replacement:
        checkpoint = replacement.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = replacement.client.post(f"/api/runs/{original['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-published-001"})
        assert response.status_code == 202, response.text
        resumed = response.json()
        assert resumed["artifacts"] == []
        assert resumed["inherited_artifacts"] == [artifact]
        wait_event(replacement.client, resumed["id"], "run.completed")
        final = replacement.client.get(f"/api/runs/{resumed['id']}").json()
        assert final["status"] == "completed" and final["inherited_artifacts"] == [artifact]
        assert final["usage"] == {"model_calls": 1, "tool_calls": 0}
        assert replacement.client.get(artifact["download_url"]).content == raw
        events = replacement.client.get(f"/api/runs/{resumed['id']}/events").json()["events"]
        assert not any(event["kind"] in {"tool.dispatched", "artifact.published"} for event in events)

@pytest.mark.parametrize(("change", "status", "code"), [("digest", 409, "checkpoint_changed"), ("source", 409, "checkpoint_sources_changed"),
    ("missing_source", 409, "source_forbidden"), ("extra_scope", 422, None)])
def test_resume_rejects_changed_checkpoint_or_scope_without_creating_run(tmp_path: Path, change: str, status: int, code: str | None) -> None:
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task
    source = tmp_path / "source"
    source.mkdir()
    path = source / "facts.md"
    path.write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, hold_first_tool="source_read")) as first:
        original = create_task(first, source)
        wait_event(first, original["id"], "validation.tool_held")
    with TestClient(create_app(store)) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        body = {"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-invalid-001"}
        if change == "digest":
            body["checkpoint_sha256"] = "0" * 64
        elif change == "source":
            path.write_bytes(b"Revenue was 99.\n")
        elif change == "missing_source":
            path.unlink()
        else:
            body["sources"] = ["private.md"]
        response = replacement.post(f"/api/runs/{original['id']}/resume", json=body)
        assert response.status_code == status, response.text
        if code:
            assert response.json()["detail"]["code"] == code
        assert len(replacement.get(f"/api/sessions/{original['session_id']}/runs").json()) == 1


def test_resume_rechecks_preserved_artifact_after_checkpoint_was_created(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, hold_first_tool="artifact_write")) as first:
        original = create_task(first, source)
        wait_event(first, original["id"], "validation.tool_held")
    with TestClient(create_app(store)) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        assert checkpoint["blockers"] == []
        artifact = checkpoint["artifacts"][0]
        (store / "artifacts" / artifact["id"]).write_bytes(b"Changed after checkpoint.\n")
        response = replacement.post(f"/api/runs/{original['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-stale-file-001"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "checkpoint_artifact_unavailable"
        assert len(replacement.get(f"/api/sessions/{original['session_id']}/runs").json()) == 1


def test_resume_preserves_ancestors_across_a_second_process_interruption(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "source_read") as first:
        original = create_process_task(first.client, source)
        wait_event(first.client, original["id"], "validation.tool_held")
        stop(first.process)
    with running_service(store, "--hold-first-tool", "artifact_write") as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.client.post(f"/api/runs/{original['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-chain-first"})
        assert response.status_code == 202, response.text
        middle = response.json()
        wait_event(second.client, middle["id"], "validation.tool_held")
        stop(second.process)
    with running_service(store) as third:
        checkpoint = third.client.get(f"/api/runs/{middle['id']}/checkpoint").json()
        assert [call["tool"] for call in checkpoint["confirmed"]] == ["workspace_list", "source_read", "artifact_write"]
        response = third.client.post(f"/api/runs/{middle['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-chain-second"})
        assert response.status_code == 202, response.text
        resumed = response.json()
        wait_event(third.client, resumed["id"], "run.completed")
        final = third.client.get(f"/api/runs/{resumed['id']}").json()
        assert final["status"] == "completed"
        assert final["usage"] == {"model_calls": 1, "tool_calls": 0}
        assert len(final["inherited_artifacts"]) == 1
        assert third.client.get(final["inherited_artifacts"][0]["download_url"]).content.endswith(b"Revenue was 42.\n")


def test_resume_cannot_hide_an_unresolved_correction_behind_an_inherited_artifact(tmp_path: Path) -> None:
    import asyncio
    import json
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task, finish, tool_reply
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    count = 0
    async def interrupted_peer(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            return tool_reply("artifact_write", {"name": "partial.md", "content": "Partial report"}, "early")
        if count == 2:
            return tool_reply("source_read", {"path": "facts.md", "start_line": -1}, "bad")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(interrupted_peer))) as first:
        original = create_task(first, source)
        wait_event(first, original["id"], "correction.available")
    async def claims_success(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        assert any('invalid_arguments' in str(message.get("content")) for message in messages)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Done."}}]})
    with TestClient(create_app(store, model_transport=httpx.MockTransport(claims_success))) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = replacement.post(f"/api/runs/{original['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-error-001"})
        assert response.status_code == 202, response.text
        final, events = finish(replacement, response.json())
        assert final["status"] == "failed"
        assert final["error"]["code"] == "unresolved_tool_error"


def test_checkpoint_does_not_resurrect_an_already_corrected_error(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task, tool_reply
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    count = 0
    async def correcting(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            return tool_reply("source_read", {"path": "facts.md", "start_line": -1}, "bad")
        if count == 2:
            return tool_reply("source_read", {"path": "facts.md"}, "fixed")
        return tool_reply("artifact_write", {"name": "report.md", "content": "Report", "citations": ["E1"]}, "publish")
    store = tmp_path / "store"
    with TestClient(create_app(store, model_transport=httpx.MockTransport(correcting), hold_first_tool="artifact_write")) as first:
        original = create_task(first, source)
        wait_event(first, original["id"], "validation.tool_held")
    with TestClient(create_app(store)) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        assert checkpoint["pending"] == []
        assert [call["tool"] for call in checkpoint["confirmed"]] == ["source_read", "artifact_write"]


def test_resume_rejects_a_different_model_without_starting_generation(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from agent_harness_demo.model import ModelSettings
    from test_local_model import LocalPeer
    from test_workbench import create_task
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store, hold_first_tool="source_read")) as first:
        original = create_task(first, source)
        wait_event(first, original["id"], "validation.tool_held")
    peer = LocalPeer()
    with TestClient(create_app(store, model_settings=ModelSettings.local("contract-model", stream=False), model_transport=httpx.MockTransport(peer))) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = replacement.post(f"/api/runs/{original['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "resume-model-changed"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "checkpoint_model_changed"
        assert peer.report.requests == []
        assert len(replacement.get(f"/api/sessions/{original['session_id']}/runs").json()) == 1


def test_resume_interrupted_before_new_model_reply_retains_unreached_work(tmp_path: Path) -> None:
    import asyncio
    import json
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    async def batch(request: httpx.Request) -> httpx.Response:
        calls = [("catalog", "workspace_list", {}), ("read", "source_read", {"path": "facts.md"}),
                 ("publish", "artifact_write", {"name": "report.md", "content": "Report"})]
        return httpx.Response(200,json={"choices":[{"message":{"role":"assistant","content":None,"tool_calls":[
            {"id":identity,"type":"function","function":{"name":name,"arguments":json.dumps(arguments)}} for identity,name,arguments in calls]}}]})
    async def waiting(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
    store = tmp_path / "store"
    with TestClient(create_app(store,model_transport=httpx.MockTransport(batch),hold_first_tool="source_read")) as first:
        original = create_task(first,source)
        wait_event(first,original["id"],"validation.tool_held")
    with TestClient(create_app(store,model_transport=httpx.MockTransport(waiting))) as second:
        checkpoint = second.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.post(f"/api/runs/{original['id']}/resume",json={"checkpoint_sha256":checkpoint["sha256"],"submission_key":"resume-before-reply"})
        assert response.status_code == 202,response.text
        middle = response.json()
        wait_event(second,middle["id"],"model.requested")
    with TestClient(create_app(store)) as third:
        checkpoint = third.get(f"/api/runs/{middle['id']}/checkpoint").json()
        assert [(call["tool"],call["status"]) for call in checkpoint["pending"]] == [("source_read","unknown"),("artifact_write","not_dispatched")]


@pytest.mark.parametrize("changed", ["stream", "context_capacity", "thinking"])
def test_resume_requires_the_saved_model_execution_configuration(tmp_path: Path, changed: str) -> None:
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from agent_harness_demo.model import ModelSettings
    from test_local_model import LocalPeer
    from test_workbench import create_task
    from test_approval import pending_approval
    from test_session_versions import qwen_metadata
    def metadata_peer(scripted: LocalPeer) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
        async def peer(request: httpx.Request) -> httpx.Response:
            if changed == "thinking" and request.url.path == "/api/version":
                return httpx.Response(200, json={"version": "0.40.10"})
            if changed == "thinking" and request.url.path == "/api/show":
                metadata = qwen_metadata()
                metadata["capabilities"].append("thinking")
                metadata["thinking"] = {"values": [False, True], "default": True}
                return httpx.Response(200, json=metadata)
            return await scripted(request)
        return peer
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with TestClient(create_app(store,model_settings=ModelSettings.local("contract-model",stream=False),model_transport=httpx.MockTransport(metadata_peer(LocalPeer())))) as first:
        original = create_task(first,source,require_publish_approval=True)
        pending_approval(first,original)
    settings = ModelSettings(mode="local",name="contract-model",stream=changed=="stream",context_window_tokens=4096 if changed=="context_capacity" else 8192,thinking=changed=="thinking")
    peer = LocalPeer()
    with TestClient(create_app(store,model_settings=settings,model_transport=httpx.MockTransport(metadata_peer(peer)))) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = replacement.post(f"/api/runs/{original['id']}/resume",json={"checkpoint_sha256":checkpoint["sha256"],"submission_key":"resume-model-settings"})
        assert response.status_code == 409,response.text
        assert response.json()["detail"]["code"] == "checkpoint_model_changed"
        assert peer.report.requests == []


def test_reused_plan_becomes_the_latest_plan_in_the_next_checkpoint(tmp_path: Path) -> None:
    import asyncio
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import create_task, tool_reply
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    count = 0
    async def original_peer(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= 2:
            return tool_reply("plan_update",{"steps":["Read facts" if count==1 else "Inspect table"]},f"plan-{count}")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
    resumed_count = 0
    async def resumed_peer(request: httpx.Request) -> httpx.Response:
        nonlocal resumed_count
        resumed_count += 1
        if resumed_count == 1:
            return tool_reply("plan_update",{"steps":["Read facts"]},"reselect-plan")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
    store = tmp_path / "store"
    with TestClient(create_app(store,model_transport=httpx.MockTransport(original_peer))) as first:
        original = create_task(first,source)
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            events = first.get(f"/api/runs/{original['id']}/events").json()['events']
            if sum(e['kind']=='model.requested' for e in events)>=3:
                break
            time.sleep(.01)
        assert sum(event['kind']=='model.requested' for event in events) >= 3
    with TestClient(create_app(store,model_transport=httpx.MockTransport(resumed_peer))) as second:
        checkpoint = second.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = second.post(f"/api/runs/{original['id']}/resume",json={"checkpoint_sha256":checkpoint['sha256'],"submission_key":"resume-plan-reselect"})
        assert response.status_code==202,response.text
        middle=response.json()
        wait_event(second,middle['id'],'tool.reused')
    with TestClient(create_app(store)) as third:
        checkpoint = third.get(f"/api/runs/{middle['id']}/checkpoint").json()
        plans = [call['result']['steps'] for call in checkpoint['confirmed'] if call['tool']=='plan_update']
        assert plans[-1] == ['Read facts']
        events = third.get(f"/api/runs/{middle['id']}/events").json()['events']
        assert any(e['kind']=='plan.updated' and e['data']['steps']==['Read facts'] for e in events)


@pytest.mark.parametrize("checkpoint_version", [None, "8085cf5"])
def test_legacy_checkpoint_with_incomplete_confirmed_intent_blocks_resume(tmp_path: Path, checkpoint_version: str | None) -> None:
    from io import BytesIO
    from zipfile import ZipFile
    from legacy import legacy_source_zip
    archive = legacy_source_zip("3de8c3ada5cb1e9eb18fef300dba55bdd4af78e5")
    old_package = tmp_path / "previous-package"
    with ZipFile(BytesIO(archive)) as source_archive:
        source_archive.extractall(old_package)
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    store = tmp_path / "store"
    with running_service(store,package_root=old_package) as first:
        workspace = first.client.post('/api/workspaces',json={'path':str(source)}).json()
        session = first.client.post('/api/sessions',json={'workspace_id':workspace['id'],'title':'Legacy recovery'}).json()
        response = first.client.post(f"/api/sessions/{session['id']}/runs",json={'task':'Save a source snapshot.','sources':['facts.md'],'require_publish_approval':True})
        assert response.status_code==202,response.text
        original=response.json()
        wait_event(first.client,original['id'],'approval.requested')
        stop(first.process)
    saved_checkpoint = None
    if checkpoint_version is not None:
        archive = legacy_source_zip(checkpoint_version)
        checkpoint_package = tmp_path / "checkpoint-package"
        with ZipFile(BytesIO(archive)) as source_archive:
            source_archive.extractall(checkpoint_package)
        with running_service(store,package_root=checkpoint_package) as intermediate:
            saved_checkpoint = intermediate.client.get(f"/api/runs/{original['id']}/checkpoint").json()
            assert saved_checkpoint['blockers'] == []
    with running_service(store) as replacement:
        checkpoint = replacement.client.get(f"/api/runs/{original['id']}/checkpoint").json()
        assert any(item['code']=='incomplete_saved_intent' for item in checkpoint.get('resume_blockers',checkpoint['blockers']))
        if saved_checkpoint is not None:
            assert checkpoint['sha256'] == saved_checkpoint['sha256']
            assert checkpoint['blockers'] == []
        response = replacement.client.post(f"/api/runs/{original['id']}/resume",json={'checkpoint_sha256':checkpoint['sha256'],'submission_key':'legacy-incomplete'})
        assert response.status_code==409 and response.json()['detail']['code']=='checkpoint_blocked'
        assert len(replacement.client.get(f"/api/sessions/{session['id']}/runs").json())==1


def test_resume_reuses_raw_read_when_model_declares_empty_label_intent(tmp_path: Path) -> None:
    import json
    from fastapi.testclient import TestClient
    from agent_harness_demo.web import create_app
    from test_workbench import finish, tool_reply
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("# Notes\nOwner: Rina\n", encoding="utf-8", newline="\n")
    store = tmp_path / "store"
    with running_service(store, "--hold-first-tool", "artifact_write") as first:
        original = create_process_task(first.client, source)
        wait_event(first.client, original["id"], "validation.tool_held")
        old_events = first.client.get(f"/api/runs/{original['id']}/events").json()["events"]
        old_read = next(event["data"]["result"] for event in old_events if event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read")
        stop(first.process)
    requests: list[dict] = []
    async def peer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return tool_reply("source_read", {"path": "facts.md", "labels": []}, "raw-again")
        if len(requests) == 2:
            return tool_reply("source_read", {"path": "facts.md", "labels": ["Owner"]}, "query-new")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Continued with saved results."}}]})
    with TestClient(create_app(store, model_transport=httpx.MockTransport(peer))) as replacement:
        checkpoint = replacement.get(f"/api/runs/{original['id']}/checkpoint").json()
        response = replacement.post(f"/api/runs/{original['id']}/resume", json={"checkpoint_sha256": checkpoint["sha256"], "submission_key": "raw-empty-labels"})
        assert response.status_code == 202, response.text
        final, events = finish(replacement, response.json())
        assert final["status"] == "completed", final["error"]
        assert not any(event["kind"] == "tool.dispatched" and event["data"]["call_id"] == "raw-again" for event in events)
        reused = next(event["data"] for event in events if event["kind"] == "tool.reused" and event["data"]["call_id"] == "raw-again")
        assert reused["result"] == old_read and reused["arguments"] == {"path": "facts.md"}
        requested = next(event["data"] for event in events if event["kind"] == "tool.requested" and event["data"]["call_id"] == "raw-again")
        assert requested["arguments"] == {"path": "facts.md", "labels": []}
        assert json.loads(requests[1]["messages"][-1]["content"]) == old_read
        query = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["call_id"] == "query-new")
        assert query["evidence_id"] != old_read["evidence_id"]
        assert query["label_queries"] == {"complete": True, "matches": {"Owner": [{"line": 2, "value": "Rina"}]}}
        assert [event["data"]["call_id"] for event in events if event["kind"] == "tool.dispatched"] == ["query-new"]
