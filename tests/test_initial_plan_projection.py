"""The first public plan must not expose guessed schema from a local model."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_workbench import create_task, finish, tool_reply


SAFE_INITIAL_PLAN = [
    "檢查輸入。",
    "依證據作答。",
    "核對成果。",
]


@pytest.mark.parametrize("protocol", ["native", "json_tools"])
def test_initial_public_plan_projects_away_preinspection_field_guesses(
    tmp_path: Path, protocol: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_text("Revenue: 42\n", encoding="utf-8")
    ungrounded_plan = [
        "Check whether order_count exists; otherwise report it missing.",
        "Use abnormal_count and is_abnormal to calculate the requested values.",
    ]
    candidate = {"name": "snapshot.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}

    if protocol == "native":
        replies = [
            tool_reply("plan_update", {"steps": ungrounded_plan}, "plan"),
            tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
            tool_reply("artifact_write", candidate, "write"),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "已保存 snapshot.md。",
            }}]}),
        ]
    else:
        replies = [
            json_reply("plan_update", {"steps": ungrounded_plan}),
            json_reply("source_read", {"path": "facts.md", "labels": []}),
            json_reply("artifact_write", candidate),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"reply": "已保存 snapshot.md。"}, ensure_ascii=False),
            }}]}),
        ]

    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol=protocol,
                                   server_controlled_initial_plan=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run = create_task(client, source)
        final, events = finish(client, run)
        assert final["status"] == "completed", final.get("error")

    plan_event = next(event for event in events if event["kind"] == "plan.updated")
    assert plan_event["data"]["steps"] == SAFE_INITIAL_PLAN
    initial_response = next(event for event in events if event["kind"] == "model.responded")
    assert initial_response["data"]["message"]["tool_calls"][0]["function"]["arguments"] == json.dumps(
        {"steps": SAFE_INITIAL_PLAN}, ensure_ascii=False,
    )
    assert not any(term in json.dumps(events, ensure_ascii=False) for term in (
        "order_count", "abnormal_count", "is_abnormal",
    ))
    assert SAFE_INITIAL_PLAN[0] in json.dumps(peer.requests[1]["messages"], ensure_ascii=False)
    assert len(final["artifacts"]) == 1
