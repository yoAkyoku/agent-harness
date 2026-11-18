"""The workbench as a child process for the browser tests: a real server whose model is a script.

Usage: e2e_server.py <scenario> <directory> <port>

Each scenario prepares a workspace folder under <directory>, starts the workbench with the options that
scenario is about (an MCP server, a skills folder, a command hook, subagents) and answers the model's
requests from a fixed list of replies. Only the model's HTTP transport is replaced; the MCP server and
the hook are real child processes. When the script runs out, the model simply never answers again.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from agent_harness.model import ModelSettings  # noqa: E402
from agent_harness.web import create_app  # noqa: E402
from test_completion import CompletionPeer  # noqa: E402
from test_workbench import tool_reply  # noqa: E402

SEARCH_NOTES = "mcp__notes__search_notes"
CREATE_NOTE = "mcp__notes__create_note"
LOG = ["# Yearly log", *(f"Note {week}: nothing relevant happened in week {week} of the project." for week in range(1, 71)),
       "Incident: checkout was down for 47 minutes.", "Handled by: Tsai of the platform team.",
       *(f"Note {week}: nothing relevant happened in week {week} of the project." for week in range(71, 141))]


class Script(CompletionPeer):
    """Replies in order, a little slowly so a recording shows each step; silent once the list is used up.

    `gates` holds a reply back until a file exists, so a test can act while the model is "thinking". Every request
    the model received is written to <directory>/requests.json, so a test can check what reached the model.
    """

    def __init__(self, replies: list[httpx.Response], log: Path, gates: dict[int, Path] | None = None) -> None:
        super().__init__(replies)
        self.log, self.gates = log, gates or {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return await super().__call__(request)
        index = len(self.requests)
        self.log.write_text(json.dumps([*self.requests, json.loads(request.content)]), encoding="utf-8")
        if index >= len(self.replies):
            await asyncio.Event().wait()
        while index in self.gates and not self.gates[index].exists():
            await asyncio.sleep(.05)
        await asyncio.sleep(float(os.environ.get("E2E_REPLY_DELAY", "0")))
        return await super().__call__(request)


def write(folder: Path, files: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_bytes(text.encode("utf-8"))


def approvals(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    write(root / "workspace", {"brief.md": "# Order tracking\nOwner: Mei\nGoal: one page that shows the order status.\n",
                               "metrics.csv": "week,orders\n1,120\n2,150\n"})
    return [
        tool_reply("plan_update", {"steps": ["Read everything", "Publish"]}, "plan"),
        tool_reply("plan_update", {"steps": ["Read brief.md", "Name the owner and the goal", "Publish summary.md"]}, "plan-2"),
        tool_reply("source_read", {"path": "brief.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "summary.md", "citations": ["E1"],
                                      "content": "# Summary\n\nOwner: Mei [brief.md E1]\n\nGoal: one page that shows the order status. [brief.md E1]\n"}, "write"),
        tool_reply("task_finish", {"summary": "Saved summary.md: Mei owns order tracking."}, "finish"),
    ], {}


def mcp(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    write(root / "workspace", {"facts.md": "Revenue was 42.\n"})
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"notes": {
        "command": sys.executable, "args": [str(HERE / "mcp_notes_server.py")],
        "allowedTools": ["search_notes"], "askTools": ["create_note"]}}}), encoding="utf-8")
    return [
        tool_reply("plan_update", {"steps": ["Record the owner in the notes", "Check the note", "Publish"]}, "plan"),
        tool_reply(CREATE_NOTE, {"name": "retro", "text": "Action owner: Leo"}, "create"),
        tool_reply(SEARCH_NOTES, {"query": "Leo"}, "search"),
        tool_reply("artifact_write", {"name": "owner.md", "content": "Action owner: Leo [mcp/notes/search_notes E2]",
                                      "citations": ["E2"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved owner.md: Leo owns the action."}, "finish"),
    ], {"mcp_config": config}


def skill(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    write(root / "workspace", {"meeting.md": "# Weekly sync\nDecision: ship the tracker on Friday. Owner: Mei\n"})
    write(root / "skills" / "meeting-notes", {"SKILL.md": (
        "---\nname: meeting-notes\ndescription: >-\n  Summarise meeting notes into decisions and owners.\n"
        "  Use when a task asks for meeting minutes.\nlicense: MIT\n---\n\n# Meeting notes\n\n"
        "List each decision on its own line, followed by its owner.\n")})
    return [
        tool_reply("plan_update", {"steps": ["Load the meeting-notes skill", "Read the meeting", "Publish the minutes"]}, "plan"),
        tool_reply("skill_load", {"name": "meeting-notes"}, "skill"),
        tool_reply("source_read", {"path": "meeting.md", "labels": []}, "read"),
        tool_reply("artifact_write", {"name": "minutes.md", "citations": ["E1"],
                                      "content": "Decision: ship the tracker on Friday. Owner: Mei [meeting.md E1]"}, "write"),
        tool_reply("task_finish", {"summary": "Saved minutes.md with one decision."}, "finish"),
    ], {"skills_dir": root / "skills"}


def hook(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    write(root / "workspace", {"facts.md": "Revenue was 42.\n", "secret.md": "Do not read.\n"})
    config = root / "hooks.json"
    config.write_text(json.dumps({"hooks": [{"event": "before_tool", "tools": ["source_read"],
                                             "command": [sys.executable, str(HERE / "hook_command.py"), "deny-secret"]}]}), encoding="utf-8")
    return [
        tool_reply("plan_update", {"steps": ["Read the sources", "Publish the revenue"]}, "plan"),
        tool_reply("source_read", {"path": "secret.md", "labels": []}, "read-secret"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read-facts"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md: revenue was 42."}, "finish"),
    ], {"hooks_config": config, "allow_command_hooks": True}


def delegate(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    write(root / "workspace", {"facts.md": "Revenue was 42.\n", "log.md": "\n".join(LOG) + "\n"})
    return [
        tool_reply("plan_update", {"steps": ["Delegate the revenue question", "Search the log for the outage", "Answer"]}, "plan"),
        tool_reply("subagent_run", {"task": "What was the revenue?", "sources": ["facts.md"]}, "delegate"),
        tool_reply("source_read", {"path": "facts.md"}, "child-read"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [facts.md E1]."}, "child-finish"),
        tool_reply("source_search", {"path": "log.md", "query": "checkout down minutes"}, "search"),
        tool_reply("task_finish", {"summary": "Revenue was 42 [subagent E1]. Checkout was down for 47 minutes [log.md E2]."}, "finish"),
    ], {"allow_subagents": True}


def interrupted(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    """Before the crash: a plan and one confirmed read, then the model never answers again."""
    write(root / "workspace", {"facts.md": "Revenue was 42.\n"})
    return [
        tool_reply("plan_update", {"steps": ["Read facts.md", "Publish revenue.md"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
    ], {}


def resumed(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    """After the restart, the resumed Run: the same read again (reused, not dispatched), then the artifact."""
    return [
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read-again"),
        tool_reply("artifact_write", {"name": "revenue.md", "content": "Revenue was 42. [facts.md E1]", "citations": ["E1"]}, "write"),
        tool_reply("task_finish", {"summary": "Saved revenue.md after resuming."}, "finish"),
    ], {}


def steer(root: Path) -> tuple[list[httpx.Response], dict[str, Any], dict[int, Path]]:
    """The second reply waits for the test (root/go), so an instruction can be added while the Run works."""
    write(root / "workspace", {"facts.md": "Revenue was 42.\n", "metrics.csv": "week,orders\n1,120\n"})
    return [
        tool_reply("plan_update", {"steps": ["Read the sources", "Publish"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
    ], {}, {1: root / "go"}


def errors(root: Path) -> tuple[list[httpx.Response], dict[str, Any]]:
    """Two Runs that fail in the two ways a Run can fail on the model's account.

    The first keeps citing evidence it never read (E9): that is sent back for correction, twice, then the Run ends
    at the correction limit. The second asks for a file nobody selected: that is outside the Run's scope, so the Run
    fails at once, without a correction.
    """
    write(root / "workspace", {"facts.md": "Revenue was 42.\n"})
    bad = {"name": "revenue.md", "content": "Revenue was 42. [facts.md E9]", "citations": ["E9"]}
    return [
        tool_reply("plan_update", {"steps": ["Read facts.md", "Publish revenue.md"]}, "plan"),
        tool_reply("source_read", {"path": "facts.md", "labels": []}, "read"),
        *(tool_reply("artifact_write", bad, f"write-{attempt}") for attempt in range(3)),
        tool_reply("plan_update", {"steps": ["Read the payroll file", "Publish"]}, "plan-2"),
        tool_reply("source_read", {"path": "payroll.md", "labels": []}, "read-payroll"),
    ], {}


def fixture(root: Path) -> tuple[None, dict[str, Any]]:
    """No script: the built-in demo model, as the workbench runs when started without --model."""
    return None, {}


SCENARIOS = {"approvals": approvals, "mcp": mcp, "skill": skill, "hook": hook, "delegate": delegate, "fixture": fixture,
             "interrupted": interrupted, "resumed": resumed, "steer": steer, "errors": errors}

if __name__ == "__main__":
    name, root, port = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    replies, options, *gates = SCENARIOS[name](root)
    if replies is not None:
        script = Script(replies, root / "requests.json", gates[0] if gates else None)
        options.update(model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                       model_transport=httpx.MockTransport(script))
    uvicorn.run(create_app(root / "store", **options), host="127.0.0.1", port=port, access_log=False)
