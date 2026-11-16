"""source_search: exact retrieval inside one selected source, as an alternative to reading it whole.

A Run that opts in (`"source_search": true`) is offered `source_search {path, query}`. It ranks the
source's lines against the query (BM25 over words and CJK character pairs; no embeddings, no index on
disk) and returns the best lines with their neighbours, word for word, with their line numbers. The
result is evidence for the same file version as `source_read`, so citations and verifiers work
unchanged, and a source larger than the context window can be used without a lossy summary.

It is retrieval, so it can miss. A query with no match is still evidence, of exactly what was
checked: that these words occur on no line of this file version. It says that this is not proof the
source lacks the information. With search offered, a `source_read` that cannot fit the room left in
the context window is refused with a reason, since it could only fail the Run one step later.
"""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness import search
from agent_harness.contracts import RunFailure
from agent_harness.model import ModelSettings
from agent_harness.tools import Tools
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

FILLER = [f"Note {number}: nothing relevant happened in week {number} of the project." for number in range(1, 141)]
LINES = ["# Yearly log", *FILLER[:70], "Incident: checkout was down for 47 minutes.", "Handled by: Tsai of the platform team.",
         *FILLER[70:]]
BIG = ("\n".join(LINES) + "\n").encode()
LIMITS = {"context_window_tokens": 6000, "max_output_tokens": 256}


def _client(tmp_path: Path, replies: list[httpx.Response]) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))), peer


def _start(client: TestClient, tmp_path: Path, **body: object) -> httpx.Response:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "log.md").write_bytes(BIG)
    (source / "other.md").write_bytes(b"Not selected.\n")
    workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    return client.post(f"/api/sessions/{session['id']}/runs",
                       json={"task": "How long was checkout down?", "sources": ["log.md"], "limits": LIMITS, **body})


def _offered(request: dict) -> set[str]:
    return {tool["function"]["name"] for tool in request.get("tools") or []}


def _results(events: list[dict], tool: str) -> list[dict]:
    return [event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == tool]


def test_a_large_source_is_searched_instead_of_read_and_the_lines_are_exact_evidence(tmp_path: Path) -> None:
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Search the log", "Answer"]}, "plan"),
        tool_reply("source_search", {"path": "log.md", "query": "checkout down minutes"}, "search"),
        tool_reply("task_finish", {"summary": "Checkout was down for 47 minutes [log.md E1]."}, "finish"),
    ])
    with app as client:
        response = _start(client, tmp_path, source_search=True, completion_workflow="answer")
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())

    assert final["status"] == "completed", final["error"]
    assert final["policy"]["source_search"] is True
    result = _results(events, "source_search")[0]
    assert result["evidence_id"] == "E1" and result["path"] == "log.md" and result["query"] == "checkout down minutes"
    assert result["sha256"] == hashlib.sha256(BIG).hexdigest()  # the whole file's version, as for source_read
    assert result["lines"] == [71, 72, 73] and result["line_start"] == 71 and result["line_end"] == 73
    assert result["text"] == "\n".join(LINES[70:73])  # word for word, one line of context on each side
    assert result["matched_lines"] == 1 and result["total_lines"] == len(LINES) and result["truncated"] is False
    assert "source_search" in _offered(peer.requests[1]) and "source_search" not in _offered(peer.requests[0])
    cited = next(event["data"] for event in events if event["kind"] == "answer.verified")["cited"]
    assert cited == [{"evidence_id": "E1", "path": "log.md", "sha256": hashlib.sha256(BIG).hexdigest(),
                      "search": {"query": "checkout down minutes", "matched_lines": 1}}]
    assert all(event["kind"] != "context.limited" for event in events)  # the 9 KB log never entered the request


def test_reading_the_same_source_whole_does_not_fit_and_search_is_not_offered_by_default(tmp_path: Path) -> None:
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read the log", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "log.md", "labels": []}, "read"),
    ])
    with app as client:
        final, _ = finish(client, _start(client, tmp_path, completion_workflow="answer").json())

    assert final["status"] == "failed" and final["error"]["code"] == "context_limit_exceeded"
    assert "source_search" not in final["policy"]
    assert all("source_search" not in _offered(request) for request in peer.requests)


def test_no_match_is_evidence_of_what_was_searched_not_of_absence(tmp_path: Path) -> None:
    """A search is deterministic, so "these words occur on no line of this file version" is a checkable fact.

    It can be cited, which lets a Run answer honestly that it found nothing; the record says it is not
    proof that the source lacks the information.
    """
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Search", "Answer"]}, "plan"),
        tool_reply("source_search", {"path": "log.md", "query": "earthquake"}, "miss"),
        tool_reply("source_search", {"path": "log.md", "query": "Handled by", "context": 0}, "hit"),
        tool_reply("task_finish", {"summary": "No line mentions an earthquake [log.md E1]. The incident was handled by "
                                              "Tsai of the platform team [log.md E2]."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path, source_search=True, completion_workflow="answer").json())

    assert final["status"] == "completed", final["error"]
    miss, hit = _results(events, "source_search")
    assert miss["evidence_id"] == "E1" and miss["matched_lines"] == 0 and miss["lines"] == [] and miss["text"] == ""
    assert miss["sha256"] == hashlib.sha256(BIG).hexdigest() and miss["query"] == "earthquake"
    assert (miss["line_start"], miss["line_end"]) == (1, len(LINES))  # what was searched: the whole file
    assert "not proof" in miss["note"]
    assert hit["evidence_id"] == "E2" and hit["lines"] == [73] and hit["text"] == "Handled by: Tsai of the platform team."
    cited = next(event["data"] for event in events if event["kind"] == "answer.verified")["cited"]
    assert cited[0]["search"] == {"query": "earthquake", "matched_lines": 0}
    assert cited[1]["search"] == {"query": "Handled by", "matched_lines": 1}


def test_a_read_that_cannot_fit_is_refused_and_the_model_searches_instead(tmp_path: Path) -> None:
    """With search offered, reading a source larger than the room left would only fail the Run later."""
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Read the log", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "log.md", "labels": []}, "read"),
        tool_reply("source_search", {"path": "log.md", "query": "checkout down"}, "search"),
        tool_reply("task_finish", {"summary": "Checkout was down for 47 minutes [log.md E1]."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path, source_search=True, completion_workflow="answer").json())

    assert final["status"] == "completed", final["error"]
    failed = next(event["data"] for event in events if event["kind"] == "tool.failed")
    assert failed["tool"] == "source_read" and failed["code"] == "source_too_large_for_context"
    correction = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert correction["error"]["retryable"] is True and "source_search" in correction["error"]["message"]
    assert str(len(BIG)) in correction["error"]["message"]
    assert _results(events, "source_search")[0]["evidence_id"] == "E1"  # the refused read left no evidence
    assert all(event["kind"] != "context.limited" for event in events)


def test_with_compaction_too_a_large_read_goes_ahead_and_is_summarised(tmp_path: Path) -> None:
    from test_compaction import CompactingPeer

    peer = CompactingPeer([
        tool_reply("plan_update", {"steps": ["Read the log", "Answer"]}, "plan"),
        tool_reply("source_read", {"path": "log.md", "labels": []}, "read"),
        tool_reply("task_finish", {"summary": "Checkout was down for 47 minutes [log.md E1]."}, "finish"),
    ])
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        final, events = finish(client, _start(client, tmp_path, source_search=True, context_compaction=True,
                                              completion_workflow="answer").json())

    assert final["status"] == "completed", final["error"]
    assert not any(event["kind"] == "tool.failed" for event in events)
    assert any(event["kind"] == "context.compacted" for event in events)


def test_only_selected_sources_can_be_searched(tmp_path: Path) -> None:
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Search"]}, "plan"),
        tool_reply("source_search", {"path": "other.md", "query": "selected"}, "search"),
    ])
    with app as client:
        final, _ = finish(client, _start(client, tmp_path, source_search=True).json())
    assert final["status"] == "failed" and final["error"]["code"] == "source_forbidden"


def test_search_needs_a_real_model(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store")) as client:
        refused = _start(client, tmp_path, source_search=True, limits={})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "source_search_unavailable"


def test_restored_search_evidence_must_match_the_selected_source_version(tmp_path: Path) -> None:
    sha = hashlib.sha256(BIG).hexdigest()
    result = {"evidence_type": "source_search", "path": "log.md", "sha256": sha, "query": "checkout", "lines": [72],
              "text": LINES[71], "line_start": 72, "line_end": 72, "matched_lines": 1, "total_lines": len(LINES),
              "truncated": False, "evidence_id": "E1"}

    def tools(source_sha: str) -> Tools:
        return Tools(None, "run", tmp_path, [{"path": "log.md", "sha256": source_sha, "bytes": len(BIG)}],  # type: ignore[arg-type]
                     search=True)

    restored = tools(sha)
    restored.restore_evidence([{"tool": "source_search", "result": result}])
    assert restored.evidence["E1"]["lines"] == [72]
    with pytest.raises(RunFailure) as raised:
        tools("f" * 64).restore_evidence([{"tool": "source_search", "result": result}])
    assert raised.value.code == "checkpoint_evidence_invalid"


def test_ranking_prefers_lines_with_rare_query_terms_and_handles_chinese() -> None:
    text = "\n".join([
        "第 1 週：訂單系統例行檢查完成，無異常。",
        "第 2 週：庫存系統例行檢查完成，無異常。",
        "第 2 週補充：支付閘道憑證更新造成結帳中斷 47 分鐘，負責處理的是平台組 蔡宗翰。",
        "第 3 週：會員系統例行檢查完成，無異常。",
        "",
        "第 4 週補充：快取叢集升級失敗造成搜尋中斷 58 分鐘。",
    ])
    found = search.search_lines(text, "結帳中斷了幾分鐘", max_results=1, context=0)
    assert found["lines"] == [3] and found["matched_lines"] == 2  # line 6 matched too, but one result was asked for
    both = search.search_lines(text, "中斷 分鐘", max_results=5, context=0)
    assert both["lines"] == [3, 6] and both["text"].count("\n") == 1 and both["total_lines"] == 6
    assert search.search_lines(text, "earthquake", max_results=5, context=1)["lines"] == []
    assert search.tokens("Wi-Fi 6 路由器") == ["wi", "fi", "6", "路由", "由器"]
    assert search.tokens("中") == ["中"] and search.tokens("!!!") == []


def test_results_are_capped_and_say_so() -> None:
    text = "\n".join(f"alpha line {number} " + "x" * 400 for number in range(1, 61))
    found = search.search_lines(text, "alpha", max_results=20, context=0)
    assert found["truncated"] is True and found["matched_lines"] == 60
    assert len(found["text"].encode()) <= search.MAX_RESULT_BYTES and len(found["lines"]) < 20
    assert found["lines"] == sorted(found["lines"])


@pytest.mark.parametrize("arguments", [
    {"path": "log.md", "query": ""}, {"path": "log.md", "query": "!!!"}, {"path": "log.md", "query": "x", "max_results": 99},
    {"path": "log.md"}, {"path": "log.md", "query": "x", "extra": 1},
])
def test_invalid_search_arguments_are_refused(tmp_path: Path, arguments: dict) -> None:
    from agent_harness.contracts import ToolCall

    tools = Tools(None, "run", tmp_path, [{"path": "log.md", "sha256": "0" * 64, "bytes": 1}], search=True)  # type: ignore[arg-type]
    with pytest.raises(RunFailure) as raised:
        tools.validate(ToolCall("c", "source_search", arguments))
    assert raised.value.code == "invalid_arguments"
    json.dumps(arguments)


def test_matching_lines_are_packed_before_their_neighbours() -> None:
    text = "\n".join(["filler one", "alpha beta gamma", "filler two", "filler three", "alpha only", "filler four"])
    roomy = search.search_lines(text, "alpha beta", max_results=5, context=1)
    assert roomy["lines"] == [1, 2, 3, 4, 5, 6] and roomy["truncated"] is False
    tight = search.search_lines(text, "alpha beta", max_results=5, context=1, limit_bytes=30)
    assert tight["lines"] == [2, 5] and tight["truncated"] is True  # both matches, no room for neighbours
    tighter = search.search_lines(text, "alpha beta", max_results=5, context=1, limit_bytes=18)
    assert tighter["lines"] == [2] and tighter["truncated"] is True  # the weaker match is dropped first


def test_a_search_result_fits_the_room_left_and_identical_searches_share_evidence(tmp_path: Path) -> None:
    import asyncio

    (tmp_path / "log.md").write_bytes(BIG)
    source = {"path": "log.md", "sha256": hashlib.sha256(BIG).hexdigest(), "bytes": len(BIG)}
    tools = Tools(None, "run", tmp_path, [source], search=True)  # type: ignore[arg-type]
    wide = {"path": "log.md", "query": "nothing relevant happened", "max_results": 20, "context": 3}

    tools.read_limit_bytes = 1500  # what the loop sets: the room left after the last fitted request
    first = asyncio.run(tools._search(wide))
    assert len(first["text"].encode()) <= 1500 - 512 and first["truncated"] is True and first["matched_lines"] == 140
    with pytest.raises(RunFailure) as repeated:  # repeating it cannot find anything new, so it is refused
        asyncio.run(tools._search(wide))
    assert repeated.value.code == "repeated_search" and "evidence E1" in repeated.value.message
    assert repeated.value.correctable_fields == ("query",) and list(tools.evidence) == ["E1"]

    miss = asyncio.run(tools._search({"path": "log.md", "query": "earthquake"}))
    assert miss["evidence_id"] == "E2"
    with pytest.raises(RunFailure, match="0 matching lines"):
        asyncio.run(tools._search({"path": "log.md", "query": "earthquake"}))
    assert list(tools.evidence) == ["E1", "E2"]

    tools.read_limit_bytes = None  # compaction on, or search not limited: the fixed cap applies
    unlimited = asyncio.run(tools._search({**wide, "context": 2}))
    assert len(unlimited["text"].encode()) > 1500 - 512 and unlimited["truncated"] is False


def test_a_repeated_search_is_refused_and_the_run_can_still_finish_or_stops_early(tmp_path: Path) -> None:
    """A model that repeats a search gets a refusal it can act on; one that keeps repeating runs out of corrections."""
    miss = tool_reply("source_search", {"path": "log.md", "query": "earthquake"}, "miss")
    app, _ = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Search", "Answer"]}, "plan"), miss,
        tool_reply("source_search", {"path": "log.md", "query": "earthquake"}, "again"),
        tool_reply("task_finish", {"summary": "No line of the log mentions an earthquake [log.md E1]."}, "finish"),
    ])
    with app as client:
        final, events = finish(client, _start(client, tmp_path, source_search=True, completion_workflow="answer").json())
    assert final["status"] == "completed", final["error"]
    failed = next(event["data"] for event in events if event["kind"] == "tool.failed")
    assert (failed["call_id"], failed["code"]) == ("again", "repeated_search")
    correction = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert correction["error"]["retryable"] is True and "evidence E1" in correction["error"]["message"]

    looping = [tool_reply("plan_update", {"steps": ["Search"]}, "plan"), miss,
               *(tool_reply("source_search", {"path": "log.md", "query": "earthquake"}, f"loop-{index}") for index in range(3))]
    app, _ = _client(tmp_path / "loop", looping)
    with app as client:
        final, events = finish(client, _start(client, tmp_path, source_search=True, completion_workflow="answer").json())
    assert final["status"] == "failed" and final["error"]["code"] == "correction_limit_exceeded"
    assert final["usage"]["model_calls"] == 5  # stopped by the correction budget, well before the step limit
