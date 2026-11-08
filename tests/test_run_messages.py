"""A user can add a message to a running Run; it reaches the model at the next decision.

Messages are admitted idempotently by message_id, delivered at step boundaries as user
messages, logged as events, replayed in later history, and reported when a Run ends
before they could be delivered.
"""
import asyncio
import json
from pathlib import Path
from threading import Event

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

ADDED = "Also mention the revenue figure."


def _replies() -> list[httpx.Response]:
    return [
        tool_reply("plan_update", {"steps": ["Read", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]",
                                      "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved snapshot.md with revenue 42."}, "finish"),
    ]


def _held_app(tmp_path: Path, peer: CompletionPeer, hold_request: int) -> tuple[TestClient, Event, Event]:
    """Hold the model's `hold_request`-th generation until the test releases it."""
    reached, release = Event(), Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and len(peer.requests) == hold_request - 1:
            reached.set()
            assert await asyncio.to_thread(release.wait, 10), "Test did not release the model."
        return await peer(request)

    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return (TestClient(create_app(tmp_path / "store", model_settings=settings,
                                  model_transport=httpx.MockTransport(transport))), reached, release)


def _start(client: TestClient, tmp_path: Path) -> tuple[dict, dict]:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "facts.md").write_bytes(b"Revenue was 42.\n")
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    run = client.post(f"/api/sessions/{session['id']}/runs",
                      json={"task": "Save a source snapshot.", "sources": ["facts.md"]}).json()
    return session, run


def test_message_added_while_running_reaches_the_next_decision(tmp_path: Path) -> None:
    peer = CompletionPeer(_replies())
    app, reached, release = _held_app(tmp_path, peer, hold_request=2)
    with app as client:
        session, run = _start(client, tmp_path)
        assert reached.wait(10)
        added = client.post(f"/api/runs/{run['id']}/messages", json={"message_id": "m-1", "text": ADDED})
        assert added.status_code == 202, added.text
        assert added.json()["status"] == "queued"
        # Re-sending the same message is idempotent; reusing its ID for other text is a conflict.
        again = client.post(f"/api/runs/{run['id']}/messages", json={"message_id": "m-1", "text": ADDED})
        assert again.status_code == 202 and again.json()["message_id"] == "m-1"
        conflict = client.post(f"/api/runs/{run['id']}/messages", json={"message_id": "m-1", "text": "Other."})
        assert conflict.status_code == 409
        release.set()
        final, events = finish(client, run)

        assert final["status"] == "completed", final["error"]
        kinds = [event["kind"] for event in events]
        assert kinds.count("message.queued") == 1
        delivered = next(event["data"] for event in events if event["kind"] == "message.delivered")
        assert delivered["message_id"] == "m-1" and delivered["step"] == 3
        # The request that was already being generated is unchanged; the next one carries the message last.
        assert not any(ADDED in str(message.get("content")) for message in peer.requests[1]["messages"])
        last = peer.requests[2]["messages"][-1]
        assert last["role"] == "user" and ADDED in last["content"]

        # A revision of this Run sees the added message in its history.
        peer.replies = _replies()
        peer.requests.clear()
        follow = client.post(f"/api/sessions/{session['id']}/runs", json={
            "task": "Revise it.", "sources": ["facts.md"],
            "base_artifact": {"run_id": run["id"], "artifact_id": final["artifacts"][0]["id"]},
        }).json()
        finish(client, follow)
        assert any(message["role"] == "user" and ADDED in str(message.get("content"))
                   for message in peer.requests[0]["messages"])


def test_message_left_undelivered_is_reported_when_the_run_ends(tmp_path: Path) -> None:
    peer = CompletionPeer(_replies())
    app, reached, release = _held_app(tmp_path, peer, hold_request=4)  # the finishing decision
    with app as client:
        _, run = _start(client, tmp_path)
        assert reached.wait(10)
        assert client.post(f"/api/runs/{run['id']}/messages",
                           json={"message_id": "late", "text": ADDED}).status_code == 202
        release.set()
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    undelivered = next(event["data"] for event in events if event["kind"] == "message.undelivered")
    assert undelivered["message_ids"] == ["late"] and undelivered["reason"] == "completed"
    assert not any(event["kind"] == "message.delivered" for event in events)


def test_messages_are_refused_once_a_run_has_ended(tmp_path: Path) -> None:
    peer = CompletionPeer(_replies())
    app, _, release = _held_app(tmp_path, peer, hold_request=99)
    with app as client:
        _, run = _start(client, tmp_path)
        final, _ = finish(client, run)
        assert final["status"] == "completed", final["error"]
        response = client.post(f"/api/runs/{run['id']}/messages", json={"message_id": "too-late", "text": ADDED})
        assert response.status_code == 409
        assert client.post("/api/runs/unknown/messages", json={"message_id": "x", "text": ADDED}).status_code == 404
        invalid = client.post(f"/api/runs/{run['id']}/messages", json={"message_id": "bad id!", "text": ADDED})
        assert invalid.status_code == 422


def test_checkpoint_keeps_a_delivered_message_for_resume(tmp_path: Path) -> None:
    from test_recovery import wait_event

    peer = CompletionPeer(_replies())
    reached, release = Event(), Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions" and not peer.requests:
            reached.set()
            assert await asyncio.to_thread(release.wait, 10)
        return await peer(request)

    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    store = tmp_path / "store"
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(transport),
                               hold_first_tool="source_read")) as first:
        _, run = _start(first, tmp_path)
        assert reached.wait(10)
        assert first.post(f"/api/runs/{run['id']}/messages",
                          json={"message_id": "keep", "text": ADDED}).status_code == 202
        release.set()
        wait_event(first, run["id"], "validation.tool_held")  # interrupted after the message was delivered
    with TestClient(create_app(store, model_settings=settings, model_transport=httpx.MockTransport(peer))) as second:
        checkpoint = second.get(f"/api/runs/{run['id']}/checkpoint").json()
        assert any(message["role"] == "user" and ADDED in message["content"] for message in checkpoint["messages"])


def test_scripted_fixture_keeps_its_recipe_when_a_message_is_added(tmp_path: Path) -> None:
    from test_recovery import wait_event

    with TestClient(create_app(tmp_path / "store", hold_first_tool="source_read")) as client:
        _, run = _start(client, tmp_path)
        wait_event(client, run["id"], "validation.tool_held")
        assert client.post(f"/api/runs/{run['id']}/messages",
                           json={"message_id": "fixture", "text": ADDED}).status_code == 202
        held = next(event["data"] for event in client.get(f"/api/runs/{run['id']}/events").json()["events"]
                    if event["kind"] == "validation.tool_held")
        assert client.post(f"/api/runs/{run['id']}/validation/{held['call_id']}/release").status_code == 200
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    assert any(event["kind"] == "message.delivered" for event in events)
