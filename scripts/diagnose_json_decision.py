"""One synthetic local generation; no tools, workbench Runs or quality acceptance."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import time
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from agent_harness.contracts import RunFailure
from agent_harness.context import request_budget
from agent_harness.implementation import source_identity
from agent_harness.model import ModelAdapter, ModelSettings
from validate_local_model import save


async def diagnose(output: Path) -> None:
    # An existing directory is never resumed: an uncertain outcome must be inspected.
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    settings = ModelSettings.local("qwen3.5:4b-q4_K_M", thinking=True, decision_protocol="json_tools")
    messages = [{"role": "system", "content": "Synthetic format diagnostic. No tool will be executed; do not claim a file was saved."},
                {"role": "user", "content": "List one concise step for checking a document by requesting probe_plan."}]
    tools: list[dict[str, Any]] = [{"type": "function", "function": {"name": "probe_plan", "description": "Return a diagnostic plan; no effect.",
        "parameters": {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"},
            "minItems": 1, "maxItems": 3}}, "required": ["steps"], "additionalProperties": False}}}]
    async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
        adapter = ModelAdapter(client, settings, transport_fixture=False)
        await adapter.prepare()
        assert adapter.info["digest"] == "d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04"
        assert adapter.info["server_version"] == "0.40.10"
        body = adapter.request_body(messages, tools, 1024)
        budget = request_budget(body, 8192, 1024, adapter.info["context_profile"])
        assert budget["upper_bound_tokens"] <= 8192
        revision = await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=root, text=True)
        implementation = await asyncio.to_thread(source_identity, root / "src/agent_harness")
        await asyncio.to_thread(save, output / "request.json", body)
        await asyncio.to_thread(save, output / "manifest.json", {"scope": "Synthetic one-generation format diagnostic, not a quality case. No tool execution or workbench Run.",
            "git_revision": revision.strip(),
            "implementation": implementation, "model": adapter.info,
            "context_budget": budget, "limits": {"model_calls": 1, "max_output_tokens": 1024, "timeout_seconds": 180},
            "policy": "No retries. Inspect the existing directory after any uncertain outcome; never count this as six-case quality evidence."})
        started = time.monotonic()
        try:
            async with asyncio.timeout(180):
                decision = await adapter.decide(messages, tools, max_output_tokens=1024)
            result: dict[str, Any] = {"status": "response_decoded", "message": decision.message, "usage": decision.usage}
            result["decision_schema_valid"] = Draft202012Validator(body["response_format"]["json_schema"]["schema"]).is_valid(
                json.loads(decision.message["wire_content"]))
            result["tool_arguments_valid"] = all(Draft202012Validator(tools[0]["function"]["parameters"]).is_valid(call.arguments)
                                                 for call in decision.calls)
        except RunFailure as error:
            result = {"status": "failed", "error": {"code": error.code, "message": error.message, "diagnostics": error.diagnostics}}
        except TimeoutError:
            result = {"status": "failed", "error": {"code": "diagnostic_deadline_exceeded"}}
        result.update(elapsed_seconds=round(time.monotonic() - started, 2), quality_accepted=False, tool_executions=0)
        await asyncio.to_thread(save, output / "result.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new evidence directory; never reuse a prior attempt.")
    args = parser.parse_args()
    asyncio.run(diagnose(args.output.resolve()))


if __name__ == "__main__":
    main()
