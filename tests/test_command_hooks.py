"""Command hooks: local programs run without a shell, enabled only with --allow-command-hooks.

`command` is a list of program arguments started directly (no shell parsing). The payload arrives
as JSON on stdin; a `before_tool` hook answers on stdout like a webhook. A non-zero exit, a timeout
or invalid output counts as a deny. The program gets a minimal environment, plus only the variables
named in `passEnv`.
"""
import json
from pathlib import Path
import sys

import pytest

from test_hooks import _replies, _run

HOOK = str(Path(__file__).with_name("hook_command.py"))


def _before(mode: str, **extra: object) -> dict:
    return {"event": "before_tool", "command": [sys.executable, HOOK, mode], "tools": ["source_read"], **extra}


def test_a_command_hook_allows_and_denies(tmp_path: Path) -> None:
    final, events, config = _run(tmp_path, [_before("deny-secret")], _replies("secret.md", "facts.md"), allow_command_hooks=True)

    assert final["status"] == "completed", final["error"]
    decided = [(event["data"]["tool"], event["data"]["decision"], event["data"].get("reason")) for event in events
               if event["kind"] == "hook.decided"]
    assert decided == [("source_read", "deny", "secret.md is off limits for reports."), ("source_read", "allow", None)]
    assert not any(event["kind"] == "tool.dispatched" and event["data"]["arguments"].get("path") == "secret.md" for event in events)
    assert config["hooks"] == [{"event": "before_tool", "command": [sys.executable, HOOK, "deny-secret"], "tools": ["source_read"]}]


@pytest.mark.parametrize(("mode", "reason"), [("sleep", "timed out"), ("fail", "exited with status 3")])
def test_a_command_hook_that_fails_or_hangs_denies(tmp_path: Path, mode: str, reason: str) -> None:
    final, events, _ = _run(tmp_path, [_before(mode, timeoutSeconds=1)], _replies("facts.md", "facts.md", "facts.md")[:4],
                            allow_command_hooks=True)

    assert final["status"] == "failed" and final["error"]["code"] == "correction_limit_exceeded"
    reasons = [event["data"]["reason"] for event in events if event["kind"] == "hook.decided"]
    assert reasons and all(reason in item for item in reasons)


def test_a_command_hook_sees_only_the_variables_it_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOK_SECRET", "s3cret")
    hidden = _run(tmp_path / "hidden", [_before("env")], _replies("facts.md", "facts.md", "facts.md")[:4], allow_command_hooks=True)[1]
    shown = _run(tmp_path / "shown", [_before("env", passEnv=["HOOK_SECRET"])], _replies("facts.md", "facts.md", "facts.md")[:4],
                 allow_command_hooks=True)[1]

    assert {event["data"]["reason"] for event in hidden if event["kind"] == "hook.decided"} == {"hidden"}
    assert {event["data"]["reason"] for event in shown if event["kind"] == "hook.decided"} == {"visible"}


def test_after_tool_command_hooks_receive_the_payload(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    hook = {"event": "after_tool", "command": [sys.executable, HOOK, "log", str(log)], "tools": ["source_read"]}
    final, events, _ = _run(tmp_path, [hook], _replies("facts.md"), allow_command_hooks=True)

    assert final["status"] == "completed", final["error"]
    logged = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [item["tool"] for item in logged] == ["source_read"] and logged[0]["result"]["path"] == "facts.md"
    assert [event["data"]["status"] for event in events if event["kind"] == "hook.notified"] == ["delivered"]
