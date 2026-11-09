"""Model-facing raw-read contract; factual model quality is verified separately."""
from pathlib import Path
import hashlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_workbench import create_task, finish, tool_reply


@pytest.mark.parametrize("protocol", ["native", "json_tools"])
def test_local_document_reader_needs_only_path_and_preserves_all_source_text(tmp_path: Path, protocol: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    raw = b"# Operations notes\nOwner: Lee\nGeneral prose without a colon.\n"
    (source / "notes.md").write_bytes(raw)
    requests: list[dict] = []
    metadata = CompletionPeer([])

    def reply(name: str, arguments: dict, identity: str) -> httpx.Response:
        return json_reply(name, arguments) if protocol == "json_tools" else tool_reply(name, arguments, identity)

    async def peer(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return await metadata(request)
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply("plan_update", {"steps": ["Read and save a cited source snapshot."]}, "plan")
        if len(requests) == 2:
            catalog = json.loads(body["messages"][0]["content"].split("Tool catalog:\n")[1]) if protocol == "json_tools" else body["tools"]
            read_tool = next(item["function"] for item in catalog if item["function"]["name"] == "source_read")
            assert set(read_tool["parameters"]["properties"]) == {"path"}
            assert read_tool["parameters"]["required"] == ["path"]
            assert "labels" not in read_tool["description"]
            return reply("source_read", {"path": "notes.md"}, "read")
        if len(requests) == 3:
            read = json.loads(body["messages"][-1]["content"])
            if protocol == "json_tools":
                read = read["tool_result"]["value"]
            assert read["text"].encode() == raw
            assert read["sha256"] == hashlib.sha256(raw).hexdigest()
            assert (read["line_start"], read["line_end"]) == (1, 3)
            assert "label_queries" not in read
            return reply("artifact_write", {"name": "snapshot.md", "content": read["text"] + "\n[notes.md E1]", "citations": ["E1"]}, "write")
        if protocol == "json_tools":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"reply":"Saved the snapshot."}'}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Saved the snapshot."}}]})

    with TestClient(create_app(tmp_path / "store", model_settings=ModelSettings.local("contract-model", stream=False, decision_protocol=protocol),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["notes.md"], task="Save a cited source snapshot."))
        assert run["status"] == "completed", run["error"]
        read = next(event["data"]["result"] for event in events if event["kind"] == "tool.completed" and event["data"]["tool"] == "source_read")
        assert read["text"].encode() == raw
        assert raw in client.get(run["artifacts"][0]["download_url"]).content
