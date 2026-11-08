"""Public Run test for Markdown table cells lost on rendering."""
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from agent_harness_demo.web import create_app
from scripted import planning
from test_workbench import create_task, finish, tool_reply


def test_model_can_correct_extra_markdown_table_cells_before_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Owner: Owen\n", encoding="utf8")
    calls = 0
    bad = "| Topic | Owner |\n|---|---|\n| Support | Owen | [facts.md E1] |\n"
    good = "| Topic | Owner | Source |\n|---|---|---|\n| Support | Owen | [facts.md E1] |\n"
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        messages = json.loads(request.content)["messages"]
        if calls == 1:
            return tool_reply("source_read", {"path":"facts.md"}, "read")
        if calls == 2:
            return tool_reply("artifact_write", {"name":"index.md", "content":bad, "citations":["E1"]}, "bad")
        if calls == 3:
            error = json.loads(messages[-1]["content"])["error"]
            assert error["code"] == "invalid_arguments" and error["retryable"]
            assert "table" in error["message"].lower()
            return tool_reply("artifact_write", {"name":"index.md", "content":good, "citations":["E1"]}, "fixed")
        return httpx.Response(200, json={"choices":[{"message":{"role":"assistant", "content":"Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(peer)))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        assert [e["data"]["call_id"] for e in events if e["kind"] == "tool.dispatched"] == ["scripted-plan-1", "read", "fixed"]
        assert any(e["kind"] == "correction.observed" for e in events)
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith(good)


@pytest.mark.parametrize("content", [
    "| A | B |\n|---|---|\n| left \\| right | ok |\n",
    "```markdown\n| A | B |\n|---|---|\n| one | two | three |\n```\n",
    "    | A | B |\n    |---|---|\n    | one | two | three |\n",
    "| A | B |\n|---|---|\n| one |\n",
    "| A | B |\n|---|---|\n| one | two |\n\nText | with | pipes | here\n",
    "| A | B |\n|---|not-a-delimiter|\n| one | two | three |\n",
    "| A | B |\n|---|---|\n| x | y |\n> note | one | two | three\n",
    "# heading | example\n---|---\none | two | three\n",
    "<!--\n| A | B |\n|---|---|\n| one | two | three |\n-->\n",

])
def test_valid_or_non_table_content_can_be_published(tmp_path: Path, content: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Source\n", encoding="utf8")
    calls = 0
    async def peer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply("artifact_write", {"name":"report.md", "content":content}, "publish")
        return httpx.Response(200, json={"choices":[{"message":{"role":"assistant", "content":"Saved."}}]})
    with TestClient(create_app(tmp_path / "store", model_transport=httpx.MockTransport(planning(peer)))) as client:
        run, _ = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        assert client.get(run["artifacts"][0]["download_url"]).text == content
