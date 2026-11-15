"""Context compaction: opt-in, labelled summaries of evidence text when a request cannot fit.

By default a Run whose request exceeds the context window stops (`context_limit_exceeded`) after the
lossless steps; nothing is summarised silently. With `context_compaction: true` the largest evidence
text in the model's view is replaced by a summary the same model writes chunk by chunk, labelled as
lossy with the original size. The durable event log keeps both the full tool result and the summary,
and the server-side evidence that citations and verifiers use keeps the full text. A summary that
cannot be produced, or does not shrink the text, leaves the Run stopped as before.
"""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import compaction
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

FILLER = "".join(f"Note {number}: nothing relevant happened in week {number} of the project.\n" for number in range(1, 141))
BIG = ("# Yearly log\n" + FILLER[:len(FILLER) // 2] + "Revenue was 42.\n" + FILLER[len(FILLER) // 2:]).encode()
LIMITS = {"context_window_tokens": 6000, "max_output_tokens": 256}


class CompactingPeer(CompletionPeer):
    """Scripted main replies, plus a summariser that answers every context_summary request."""

    def __init__(self, replies: list[httpx.Response], summary: str | None = "ok") -> None:
        super().__init__(replies)
        self.summary = summary
        self.summaries: list[dict] = []
        self.main: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            if [tool["function"]["name"] for tool in body.get("tools") or []] == ["context_summary"]:
                self.summaries.append(body)
                excerpt = body["messages"][-1]["content"]
                if self.summary is None:
                    return httpx.Response(500, json={"error": "summariser down"})
                text = "Revenue was 42." if "Revenue was 42." in excerpt else self.summary
                return tool_reply("context_summary", {"summary": text}, f"summary-{len(self.summaries)}")
            self.main.append(body)
        return await super().__call__(request)

    @property
    def requests_seen(self) -> int:
        return len(self.main)


def _replies() -> list[httpx.Response]:
    return [
        tool_reply("plan_update", {"steps": ["Read the log", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "log.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Revenue was 42. [log.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ]


def _run(tmp_path: Path, peer: CompactingPeer, **body: object) -> tuple[dict, list[dict], TestClient]:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "log.md").write_bytes(BIG)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    app = TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer)))
    with app as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        response = client.post(f"/api/sessions/{session['id']}/runs",
                               json={"task": "What was the revenue?", "sources": ["log.md"], "limits": LIMITS, **body})
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())
        if final["artifacts"]:
            final["content"] = client.get(f"/api/runs/{final['id']}/artifacts/{final['artifacts'][0]['id']}").text
    return final, events, app


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [event["data"] for event in events if event["kind"] == kind]


def test_by_default_an_oversized_source_stops_the_run_without_any_summary(tmp_path: Path) -> None:
    peer = CompactingPeer(_replies())
    final, events, _ = _run(tmp_path, peer)

    assert final["status"] == "failed" and final["error"]["code"] == "context_limit_exceeded"
    assert not peer.summaries and not _kinds(events, "context.compacted")
    assert "context_compaction" not in final["policy"]


def test_compaction_changes_what_the_model_reads_not_what_is_verified(tmp_path: Path) -> None:
    peer = CompactingPeer(_replies())
    final, events, _ = _run(tmp_path, peer, context_compaction=True)

    assert final["status"] == "completed", final["error"]
    assert final["policy"]["context_compaction"] is True
    compacted = _kinds(events, "context.compacted")
    assert len(compacted) == 1
    record = compacted[0]
    assert record["evidence_id"] == "E1" and record["path"] == "log.md" and record["lossy"] is True
    assert record["source_sha256"] == hashlib.sha256(BIG).hexdigest()
    assert record["model"] == "contract-model" and record["chunks"] == len(peer.summaries) >= 2
    assert record["original_text_bytes"] == len(BIG) and record["summary_bytes"] * 2 <= len(BIG)
    assert "Revenue was 42." in record["summary"] and record["summary"].startswith("[lines 1-")
    assert record["summary_sha256"] == hashlib.sha256(record["summary"].encode()).hexdigest()
    assert record["upper_bound_tokens"] <= record["capacity_tokens"] == 6000

    # The durable record of the read keeps the full text; only the model's view was replaced.
    read = next(data["result"] for data in _kinds(events, "tool.completed") if data["tool"] == "source_read")
    assert read["text"] == BIG.decode() and "compacted" not in read
    seen = json.loads(next(message["content"] for message in peer.main[2]["messages"] if message["role"] == "tool"
                           and "evidence_id" in message["content"]))
    assert "text" not in seen and seen["text_summary"] == record["summary"]
    assert seen["compacted"]["lossy"] is True and seen["compacted"]["original_text_bytes"] == len(BIG)
    assert seen["evidence_id"] == "E1" and seen["sha256"] == hashlib.sha256(BIG).hexdigest()

    # Each summary request holds one excerpt and the task, never the Run's conversation, and fits the window.
    for request in peer.summaries:
        assert [message["role"] for message in request["messages"]] == ["system", "user"]
        assert "What was the revenue?" in request["messages"][1]["content"]
    assert "".join(request["messages"][1]["content"].split(":\n", 1)[1] + "\n" for request in peer.summaries) == BIG.decode()

    # The citation still points at the whole file, and the summariser's calls are counted.
    assert f"- [E1] log.md:L1-L{len(BIG.decode().splitlines())} SHA-256 {hashlib.sha256(BIG).hexdigest()}" in final["content"]
    assert final["usage"]["model_calls"] == 4 + len(peer.summaries)
    assert len(_kinds(events, "compaction.model_responded")) == len(peer.summaries)


@pytest.mark.parametrize(("summary", "reason"), [
    (None, "model_unavailable"),
    ("This week nothing relevant happened in the project, as in every other week of the year. " * 60, "compaction_unavailable"),
])
def test_a_summary_that_fails_or_does_not_shrink_leaves_the_run_stopped(tmp_path: Path, summary: str | None, reason: str) -> None:
    peer = CompactingPeer(_replies(), summary)
    final, events, _ = _run(tmp_path, peer, context_compaction=True)

    assert final["status"] == "failed" and final["error"]["code"] == "context_limit_exceeded"
    failed = _kinds(events, "context.compaction_failed")
    assert failed and failed[0]["reason"] == reason and failed[0]["evidence_id"] == "E1"
    assert not _kinds(events, "context.compacted") and not final["artifacts"]


def test_compaction_needs_a_real_model(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "log.md").write_bytes(b"Revenue was 42.\n")
    with TestClient(create_app(tmp_path / "store")) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        refused = client.post(f"/api/sessions/{session['id']}/runs",
                              json={"task": "Snapshot.", "sources": ["log.md"], "context_compaction": True})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "context_compaction_unavailable"


def test_chunks_cover_every_line_within_the_byte_limit() -> None:
    text = "\n".join(f"line {number} " + "x" * (number % 40) for number in range(1, 200)) + "\n" + "長" * 900
    pieces = compaction.chunk_lines(text, 600)
    assert all(len(piece.encode()) <= 600 for _, _, piece in pieces)
    assert pieces[0][0] == 1 and pieces[-1][1] == 200
    lines = text.splitlines()
    rebuilt: list[str] = []
    for first, last, piece in pieces:
        if first == last and rebuilt and len(rebuilt) == first:  # a long line cut into several pieces
            rebuilt[-1] += piece
        else:
            rebuilt.extend(piece.split("\n"))
    assert rebuilt == lines


def test_only_uncompacted_evidence_text_is_a_candidate() -> None:
    big, small = "a" * 4000, "b" * 100
    messages = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "1", "content": json.dumps({"evidence_id": "E1", "path": "a.md", "text": small})},
        {"role": "tool", "tool_call_id": "2", "content": json.dumps({"evidence_id": "E2", "path": "b.md", "text": big})},
        {"role": "tool", "tool_call_id": "3", "content": json.dumps({"saved": True, "text": big * 2})},
    ]
    ledger = {"E3": {"evidence_id": "E3", "path": "c.md", "text": big * 3, "compacted": {"lossy": True}},
              "E4": {"evidence_id": "E4", "path": "d.md", "text": big + "more"}}
    kind, where, result = compaction.largest_target(messages, 1, ledger)  # type: ignore[misc]
    assert (kind, where, result["evidence_id"]) == ("ledger", "E4", "E4")
    assert compaction.largest_target(messages, 1, {})[:2] == ("message", 2)  # type: ignore[index]
    assert compaction.largest_target(messages[:2], 1, {}) is None
    view = compaction.compacted_view({"evidence_id": "E2", "path": "b.md", "sha256": "0" * 64, "text": big,
                                      "labelled_lines": [1], "line_start": 1, "line_end": 1}, "short", "m", 2)
    assert view == {"evidence_id": "E2", "path": "b.md", "sha256": "0" * 64, "line_start": 1, "line_end": 1,
                    "text_summary": "short", "compacted": {**view["compacted"], "lossy": True, "by": "m", "chunks": 2,
                                                           "original_text_bytes": 4000}}
