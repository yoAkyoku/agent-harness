"""Token usage and cost: each Run totals the usage its provider reports, and prices only come from the user.

There is no built-in price table (prices change); `--price MODEL=INPUT,OUTPUT` sets US dollars per
million tokens, and admission pins the chosen model's price into the Run, so an estimate stays the
one the Run was started under. A reply that reports no usage is counted, and its Run's cost is
marked partial rather than guessed.
"""
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.__main__ import build_prices, build_settings, parser
from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_mcp_tools import _start
from test_remote_providers import _message, _source, _tool_use
from test_workbench import finish, tool_reply

WRITE = {"name": "revenue.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}


def _with_usage(response: httpx.Response, prompt: int, completion: int) -> httpx.Response:
    return httpx.Response(200, json={**response.json(),
                                     "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                               "total_tokens": prompt + completion}})


def _replies(*, report_usage: bool = True) -> list[httpx.Response]:
    replies = [
        tool_reply("plan_update", {"steps": ["Read facts.md", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        tool_reply("artifact_write", WRITE, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ]
    if not report_usage:
        return replies
    return [_with_usage(reply, 1000 * (index + 1), 50) for index, reply in enumerate(replies)]


def _app(tmp_path: Path, replies: list[httpx.Response], **options: object) -> TestClient:
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings,
                                 model_transport=httpx.MockTransport(CompletionPeer(replies)), **options))  # type: ignore[arg-type]


def test_a_run_totals_the_tokens_its_provider_reports(tmp_path: Path) -> None:
    with _app(tmp_path, _replies()) as client:
        final, _ = finish(client, _start(client, tmp_path))

    assert final["status"] == "completed", final["error"]
    assert final["token_usage"] == {"input_tokens": 10_000, "output_tokens": 200,
                                        "reported_responses": 4, "unreported_responses": 0}
    assert "estimated_cost" not in final  # no price was configured


def test_a_configured_price_is_pinned_and_gives_an_estimate(tmp_path: Path) -> None:
    prices = {"contract-model": {"input": 3.0, "output": 15.0}}
    with _app(tmp_path, _replies(), prices=prices) as client:
        assert client.get("/api/config").json()["prices"] == prices
        run = _start(client, tmp_path)
        final, _ = finish(client, run)

    assert run["model"]["price_per_mtok"] == {"input": 3.0, "output": 15.0}
    # 10,000 input tokens at $3/M plus 200 output tokens at $15/M.
    assert final["estimated_cost"] == {"usd": pytest.approx(0.033), "partial": False}


def test_replies_without_usage_make_the_estimate_partial(tmp_path: Path) -> None:
    replies = _replies(report_usage=False)
    replies[0] = _with_usage(replies[0], 400, 30)
    with _app(tmp_path, replies, prices={"contract-model": {"input": 3.0, "output": 15.0}}) as client:
        final, _ = finish(client, _start(client, tmp_path))

    assert final["token_usage"] == {"input_tokens": 400, "output_tokens": 30,
                                        "reported_responses": 1, "unreported_responses": 3}
    assert final["estimated_cost"]["partial"] is True


def test_claude_usage_is_totalled_like_any_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-not-real")
    replies = [
        _message(_tool_use("toolu_plan", "plan_update", {"steps": ["Read", "Publish"]}), "tool_use"),
        _message(_tool_use("toolu_read", "source_read", {"path": "facts.md", "labels": []}), "tool_use"),
        _message(_tool_use("toolu_write", "artifact_write", {"name": "notes.md", "content": "Revenue was 42. [facts.md E1]",
                                                             "citations": ["E1"]}), "tool_use"),
        _message(_tool_use("toolu_finish", "task_finish", {"summary": "Saved notes.md: revenue was 42."}), "tool_use"),
    ]
    count = iter(range(len(replies)))
    settings = ModelSettings.anthropic("claude-opus-5-5", api_key_env="TEST_ANTHROPIC_KEY")
    app = create_app(tmp_path / "store", model_settings=settings,
                     anthropic_transport=httpx2.MockTransport(lambda request: replies[next(count)]),
                     prices={"claude-opus-5-5": {"input": 5.0, "output": 25.0}})
    with TestClient(app) as client:
        workspace = client.post("/api/workspaces", json={"path": str(_source(tmp_path))}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": "Save the revenue.", "sources": ["facts.md"]}).json()
        final, _ = finish(client, run)

    assert final["status"] == "completed", final["error"]
    assert final["token_usage"] == {"input_tokens": 400, "output_tokens": 80,
                                        "reported_responses": 4, "unreported_responses": 0}
    assert final["estimated_cost"] == {"usd": pytest.approx(0.004), "partial": False}


def test_cli_prices_name_configured_models_only() -> None:
    arguments = parser().parse_args(["--provider", "anthropic", "--alt-model", "claude-sonnet-5-5",
                                     "--price", "claude-opus-5-5=5,25", "--price", "claude-sonnet-5-5=3,15"])
    assert build_prices(arguments, build_settings(arguments)) == {
        "claude-opus-5-5": {"input": 5.0, "output": 25.0}, "claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}
    for bad in (["--price", "claude-opus-5-5=5"], ["--price", "claude-opus-5-5=-1,2"], ["--price", "claude-haiku-5-5=1,5"],
                ["--price", "claude-opus-5-5=a,b"], ["--price", "claude-opus-5-5=5,25", "--price", "claude-opus-5-5=5,25"]):
        arguments = parser().parse_args(["--provider", "anthropic", *bad])
        with pytest.raises(ValueError):
            build_prices(arguments, build_settings(arguments))
