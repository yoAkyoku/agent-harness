"""Headless mode: run one task on a running workbench and print its events as NDJSON.

    AGENT_HARNESS_TOKEN=... python -m agent_harness_demo.headless \\
        --workspace ./docs --source notes.md --task "Summarise the notes into summary.md"

Every saved event is printed as one JSON line, exactly as the workbench streams it,
followed by one `{"kind": "headless.result", ...}` line. The exit code reports the
outcome. The client only observes: stopping it does not stop the Run.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from .local import TOKEN_ENV

EXIT_CODES = {"completed": 0, "failed": 1, "cancelled": 3, "interrupted": 4}
REQUEST_ERROR = 2
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
RECONNECT_ATTEMPTS = 5
MAX_EVENT_BYTES = 16 * 1024 * 1024


class HeadlessError(Exception):
    """A request the workbench refused, or a setup problem; reported on stderr with exit code 2."""


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="python -m agent_harness_demo.headless",
        description="Run one task on a running workbench and print its events as NDJSON on stdout.",
        epilog=f"The access token is read from {TOKEN_ENV}. Exit codes: 0 completed, 1 failed, "
               "2 refused request or usage error, 3 cancelled, 4 interrupted.")
    cli.add_argument("--url", default="http://127.0.0.1:8787", help="Workbench address (default %(default)s).")
    cli.add_argument("--workspace", required=True, help="Folder that holds the sources.")
    cli.add_argument("--source", dest="sources", action="append", required=True,
                     help="A source file inside the workspace; repeat for more.")
    task = cli.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="The task text.")
    task.add_argument("--task-file", type=Path, help="Read the task text from a UTF-8 file.")
    cli.add_argument("--mcp-tool", dest="mcp_tools", action="append", default=[],
                     help="An allowed read-only MCP tool for this Run, such as mcp__notes__search; repeat for more.")
    cli.add_argument("--max-steps", type=int, help="Model decision limit for this Run.")
    cli.add_argument("--max-tool-calls", type=int, help="Tool call limit for this Run.")
    cli.add_argument("--submission-key", help="Re-sending the same key returns the original Run instead of starting another.")
    return cli


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise HeadlessError("--url must be a plain http(s) address such as http://127.0.0.1:8787.")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise HeadlessError("Plain http is allowed only for a loopback workbench; the token would travel unencrypted.")
    return url.rstrip("/")


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(detail, dict):
        return " ".join(str(detail.get(key, "")) for key in ("code", "message")).strip()
    if isinstance(detail, list):
        return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
    return str(detail or f"HTTP {response.status_code}")


async def _post(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
    response = await client.post(path, json=body)
    if response.status_code >= 400:
        raise HeadlessError(f"{path} refused ({response.status_code}): {_detail(response)}")
    return response.json()


async def _stream(base: str, headers: dict[str, str], run_id: str, out: TextIO) -> None:
    """Print events from the durable stream, acknowledging each; reconnect from the last cursor."""
    socket_base = "ws" + base[len("http"):]
    last = 0
    failures = 0
    while True:
        try:
            async with connect(f"{socket_base}/api/runs/{run_id}/stream?after={last}&flow=ack",
                               additional_headers=headers, open_timeout=15, max_size=MAX_EVENT_BYTES) as socket:
                async for message in socket:
                    event = json.loads(message)
                    if event["sequence"] <= last:
                        continue  # already printed before a reconnect
                    out.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                    out.flush()
                    last = event["sequence"]
                    failures = 0
                    await socket.send(json.dumps({"ack": last}))
            return  # a normal close: the Run is terminal and every event was delivered
        except (OSError, ConnectionClosedError, InvalidHandshake, TimeoutError) as error:
            failures += 1
            if failures > RECONNECT_ATTEMPTS:
                raise HeadlessError(f"Lost the event stream after {last} events: {error}") from error
            await asyncio.sleep(min(0.25 * 2 ** failures, 5))


async def run(arguments: argparse.Namespace, task: str, out: TextIO) -> int:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise HeadlessError(f"Set {TOKEN_ENV} to the access token the workbench printed when it started.")
    base = _base_url(arguments.url)
    headers = {"Authorization": f"Bearer {token}"}
    limits = {name: value for name, value in (("max_steps", arguments.max_steps),
                                              ("max_tool_calls", arguments.max_tool_calls)) if value is not None}
    body: dict[str, Any] = {"task": task, "sources": arguments.sources,
                            "submission_key": arguments.submission_key or f"headless-{uuid4().hex}"}
    if limits:
        body["limits"] = limits
    if arguments.mcp_tools:
        body["mcp_tools"] = arguments.mcp_tools
    async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False, timeout=30) as client:
        workspace = await _post(client, "/api/workspaces", {"path": str(Path(arguments.workspace).resolve())})
        session = await _post(client, "/api/sessions", {"workspace_id": workspace["id"]})
        started = await _post(client, f"/api/sessions/{session['id']}/runs", body)
        print(f"Run {started['id']} accepted; events follow on stdout.", file=sys.stderr, flush=True)
        await _stream(base, headers, started["id"], out)
        response = await client.get(f"/api/runs/{started['id']}")
        response.raise_for_status()
        final = response.json()
    summary = {"kind": "headless.result", "run": {
        "id": final["id"], "status": final["status"], "result": final.get("result"), "error": final.get("error"),
        "usage": final.get("usage"),
        "artifacts": [{"name": artifact["name"], "sha256": artifact["sha256"], "download_url": base + artifact["download_url"]}
                      for artifact in final.get("artifacts", [])],
    }}
    out.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
    out.flush()
    return EXIT_CODES.get(final["status"], EXIT_CODES["failed"])


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # NDJSON is UTF-8 whatever the console code page is
    try:
        task = arguments.task if arguments.task is not None else arguments.task_file.read_text(encoding="utf-8")
        return asyncio.run(run(arguments, task, sys.stdout))
    except HeadlessError as error:
        print(f"error: {error}", file=sys.stderr)
    except (OSError, httpx.HTTPError) as error:
        print(f"error: cannot use the workbench: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Stopped watching; the Run continues on the workbench.", file=sys.stderr)
        return 130
    return REQUEST_ERROR


if __name__ == "__main__":
    sys.exit(main())
