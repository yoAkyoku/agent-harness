"""Public boundary checks for optional summary-grounding guidance."""
import json
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_completion import CompletionPeer, publication_replies
from test_local_model import LocalPeer, qwen_thinking_metadata
from test_workbench import create_task, finish, tool_reply


def test_summary_guidance_only_after_publication_and_within_context_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Owner: Owen\n", encoding="utf8")
    requests = []
    peer = LocalPeer()
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_thinking_metadata())
        if request.url.path != "/v1/chat/completions":
            return await peer(request)
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return tool_reply("plan_update", {"steps": ["Read source", "Publish"]}, "plan")
        if len(requests) == 2:
            return tool_reply("source_read", {"path": "facts.md"}, "read")
        if len(requests) == 3:
            return tool_reply("artifact_write", {"name": "index.md", "content": "Owner: Owen [E1]", "citations": ["E1"]}, "write")
        return tool_reply("task_finish", {"summary": "Saved index.md with owner Owen."}, "finish")
    settings = ModelSettings.local("contract-model", thinking=True, stream=False)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(transport))) as client:
        run, events = finish(client, create_task(client, source, sources=["facts.md"]))
        assert run["status"] == "completed", run["error"]
        marker = "完成摘要只敘述已保存成果明確包含的內容"
        assert all(marker not in request["messages"][0]["content"] for request in requests[:-1])
        assert marker in requests[-1]["messages"][0]["content"]
        assert all(e["data"]["context_budget"]["upper_bound_tokens"] <= 8192 for e in events if e["kind"] == "model.requested")
        assert client.get(run["artifacts"][0]["download_url"]).text.startswith("Owner: Owen [E1]")


def json_decision_reply(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"calls": [{"name": name, "arguments": arguments}]}, ensure_ascii=False),
    }}]})


def test_model_request_applies_user_language_to_document_labels(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Revenue was 42.\n", encoding="utf8")
    peer = CompletionPeer(publication_replies() + [tool_reply("task_finish", {"summary": "已保存。"}, "finish")])
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run, _ = finish(client, create_task(
            client, source, task="請用繁體中文整理 facts.md，標題、欄名與說明使用繁體中文。",
            sources=["facts.md"],
        ))
        assert run["status"] == "completed", run["error"]
    prompt = peer.requests[0]["messages"][0]["content"]
    assert "標題、欄名、說明與回覆用任務語言" in prompt
    assert "來源事實可留原文" in prompt
    assert "只列要求項目" in prompt
    assert "比率按指定分子／分母；缺欄不呼叫 number_calculate" in prompt
    assert "只用來源欄名，不造欄" in prompt
    assert "格式「數值 [檔名 EID]」" in prompt
    assert "成果保持精簡" in prompt
