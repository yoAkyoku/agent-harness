"""Compare two versions of an artifact: by default a revision against its parent version."""
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.web import create_app
from test_workbench import ReportPeer, create_task, finish, tool_reply

REVISION_TASK = "Retitle the base as Revised."


def _revised(tmp_path: Path, revised_content: str = "# Revised\nRevenue was 42.\nAdded line.\n",
             original_text: bytes = b"Revenue was 42.\n") -> tuple[TestClient, dict, dict]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes(original_text)
    initial = ReportPeer()
    revision_requests: list[dict] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not any(item.get("content") == REVISION_TASK for item in body["messages"]):
            return await initial(request)
        revision_requests.append(body)
        if len(revision_requests) == 1:
            return tool_reply("plan_update", {"steps": ["Revise the title"]}, "revise-plan")
        if len(revision_requests) == 2:
            return tool_reply("artifact_write", {"name": "snapshot.md", "content": revised_content}, "revise")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Revised."}}]})

    client = TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(peer)))
    client.__enter__()
    original, _ = finish(client, create_task(client, source))
    response = client.post(f"/api/sessions/{original['session_id']}/runs", json={
        "task": REVISION_TASK, "sources": ["facts.md"],
        "base_artifact": {"run_id": original["id"], "artifact_id": original["artifacts"][0]["id"]},
    })
    revised, _ = finish(client, response.json())
    assert revised["status"] == "completed", revised["error"]
    return client, original, revised


def test_a_revision_is_compared_with_its_parent_version(tmp_path: Path) -> None:
    client, original, revised = _revised(tmp_path)
    try:
        newer = revised["artifacts"][0]
        diff = client.get(f"/api/runs/{revised['id']}/artifacts/{newer['id']}/diff").json()
    finally:
        client.__exit__(None, None, None)
    assert diff["from"]["id"] == original["artifacts"][0]["id"] and diff["from"]["version"] == 1
    assert diff["to"]["id"] == newer["id"] and diff["to"]["version"] == 2
    assert diff["identical"] is False and diff["added"] == 2 and diff["removed"] == 1
    assert "-# Snapshot" in diff["diff"] and "+# Revised" in diff["diff"] and "+Added line." in diff["diff"]
    assert "snapshot.md v1" in diff["diff"] and "snapshot.md v2" in diff["diff"]


def test_any_two_versions_in_the_session_can_be_compared_explicitly(tmp_path: Path) -> None:
    client, original, revised = _revised(tmp_path)
    try:
        older = original["artifacts"][0]
        reverse = client.get(f"/api/runs/{original['id']}/artifacts/{older['id']}/diff",
                             params={"against": revised["artifacts"][0]["id"]}).json()
        no_parent = client.get(f"/api/runs/{original['id']}/artifacts/{older['id']}/diff")
    finally:
        client.__exit__(None, None, None)
    assert reverse["from"]["version"] == 2 and reverse["to"]["version"] == 1
    assert "+# Snapshot" in reverse["diff"]
    assert no_parent.status_code == 409 and "earlier version" in no_parent.text


def test_diffs_stay_inside_one_session_and_refuse_changed_bytes(tmp_path: Path) -> None:
    client, original, revised = _revised(tmp_path)
    try:
        other_source = tmp_path / "other"
        other_source.mkdir()
        (other_source / "facts.md").write_bytes(b"Revenue was 42.\n")
        foreign, _ = finish(client, create_task(client, other_source))
        newer = revised["artifacts"][0]
        crossing = client.get(f"/api/runs/{revised['id']}/artifacts/{newer['id']}/diff",
                              params={"against": foreign["artifacts"][0]["id"]})
        (tmp_path / "store" / "artifacts" / original["artifacts"][0]["id"]).write_bytes(b"Changed outside the harness.\n")
        changed = client.get(f"/api/runs/{revised['id']}/artifacts/{newer['id']}/diff")
    finally:
        client.__exit__(None, None, None)
    assert crossing.status_code == 404
    assert changed.status_code == 409 and "hash" in changed.text


def test_removed_lines_that_look_like_file_headers_are_still_counted(tmp_path: Path) -> None:
    # The original snapshot holds a Markdown rule; removing it yields the diff line "----".
    client, original, revised = _revised(tmp_path, revised_content="# Snapshot\nRevenue was 42.\n",
                                         original_text=b"Revenue was 42.\n---\n")
    try:
        newer = revised["artifacts"][0]
        diff = client.get(f"/api/runs/{revised['id']}/artifacts/{newer['id']}/diff").json()
    finally:
        client.__exit__(None, None, None)
    assert "\n----\n" in diff["diff"]
    assert diff["removed"] == 1 and diff["added"] == 0
