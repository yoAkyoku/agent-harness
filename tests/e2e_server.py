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
    """Replies in order, a little slowly so a recording shows each step; silent once the list is used up."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            if len(self.requests) >= len(self.replies):
                await asyncio.Event().wait()
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


def fixture(root: Path) -> tuple[None, dict[str, Any]]:
    """No script: the built-in demo model, as the workbench runs when started without --model."""
    return None, {}


SCENARIOS = {"approvals": approvals, "mcp": mcp, "skill": skill, "hook": hook, "delegate": delegate, "fixture": fixture}

if __name__ == "__main__":
    name, root, port = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    replies, options = SCENARIOS[name](root)
    if replies is not None:
        options.update(model_settings=ModelSettings.local("contract-model", stream=False, thinking=True),
                       model_transport=httpx.MockTransport(Script(replies)))
    uvicorn.run(create_app(root / "store", **options), host="127.0.0.1", port=port, access_log=False)
