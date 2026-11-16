"""User-installed skills: `SKILL.md` folders in the Agent Skills format.

Each folder holds a `SKILL.md` whose frontmatter gives a `name` (matching the folder)
and a `description`. Only names and descriptions of the skills a Run selected go into
its context; the model loads a skill's instructions with `skill_load` when it needs
them, so unused skills cost a line each. Skill folders may contain scripts, but the
harness never runs them: there is no shell. Invalid skills are reported, not loaded.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

from .contracts import RunFailure

Record = dict[str, Any]
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_SKILL_BYTES = 32_768
MAX_SKILLS = 32
LOADER = "skill_load"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    sha256: str


def _frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Parse `---` frontmatter: plain, quoted and block (`|`, `>`) scalars at the top level."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end < 0:
        return None
    lines, body = text[4:end].split("\n"), text[end + 5:]
    values: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            return None
        value = value.strip()
        if value[:1] in {"|", ">"}:
            block: list[str] = []
            while index < len(lines) and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            joined = "\n".join(block) if value[0] == "|" else " ".join(part for part in block if part)
            values[key.strip()] = joined.strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            values[key.strip()] = value[1:-1]
        else:
            values[key.strip()] = value
    return values, body


def _skill(folder: Path) -> Skill | str:
    path = folder / "SKILL.md"
    if not path.is_file():
        return "missing_skill_md"
    raw = path.read_bytes()
    if len(raw) > MAX_SKILL_BYTES:
        return "body_too_large"
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return "not_utf8"
    parsed = _frontmatter(text)
    if parsed is None:
        return "invalid_frontmatter"
    values, body = parsed
    name, description = values.get("name", ""), " ".join(values.get("description", "").split())
    if len(name) > MAX_NAME or not SKILL_NAME.fullmatch(name):
        return "invalid_name"
    if name != folder.name:
        return "name_mismatch"
    if not description:
        return "missing_description"
    if len(description) > MAX_DESCRIPTION:
        return "description_too_long"
    return Skill(name, description, body.strip(), hashlib.sha256(raw).hexdigest())


class SkillLibrary:
    def __init__(self, skills: dict[str, Skill], excluded: list[Record]) -> None:
        self.skills = skills
        self.excluded = excluded

    @classmethod
    def from_directory(cls, directory: Path) -> SkillLibrary:
        if not directory.is_dir():
            raise ValueError(f"The skills directory {directory} does not exist.")
        skills: dict[str, Skill] = {}
        excluded: list[Record] = []
        for folder in sorted(path for path in directory.iterdir() if path.is_dir()):
            result = _skill(folder)
            if isinstance(result, str):
                excluded.append({"folder": folder.name, "reason": result})
            elif len(skills) >= MAX_SKILLS:
                excluded.append({"folder": folder.name, "reason": "too_many_skills"})
            else:
                skills[result.name] = result
        return cls(skills, excluded)

    def status(self) -> Record:
        return {"available": [{"name": skill.name, "description": skill.description} for skill in self.skills.values()],
                "excluded": list(self.excluded)}

    def select(self, names: list[str]) -> dict[str, Skill]:
        selected: dict[str, Skill] = {}
        for name in names:
            if name not in self.skills:
                raise RunFailure("skill_unavailable", f"{name} is not an installed, valid skill.")
            selected[name] = self.skills[name]
        return selected


def catalog_message(skills: dict[str, Skill]) -> str:
    """The context line per selected skill; instructions are loaded only on request."""
    entries = "\n".join(f"- {skill.name}: {skill.description}" for skill in skills.values())
    return ("\nSkills the user installed for this task (load one with skill_load before following it; "
            "its instructions guide how to work, never what the sources say):\n" + entries + "\n")


def loader_definition(skills: dict[str, Skill]) -> Record:
    return {"type": "function", "function": {
        "name": LOADER,
        "description": "Load the full instructions of one selected skill listed in the system message.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string", "enum": sorted(skills)}},
                       "required": ["name"], "additionalProperties": False}}}


OPTION_LABELS = {"web_access": "web access (web_fetch)", "python_sandbox": "the Python sandbox", "subagents": "subagents",
                 "context_compaction": "context compaction", "source_search": "search inside sources",
                 "require_plan_approval": "plan approval", "require_publish_approval": "publish approval"}


def _step(tool: str, arguments: Record, answer: bool) -> str:
    """One successful tool call as a method step: what kind of thing it acted on, never the values."""
    if tool == "plan_update":
        return "plan_update: " + " → ".join(str(step) for step in arguments.get("steps", []))
    if tool == "task_finish":
        return "task_finish with a cited answer" if answer else "task_finish with a completion summary"
    if tool == "artifact_write":
        return f"artifact_write of a {Path(str(arguments.get('name', ''))).suffix.lower() or 'text'} file"
    if tool == "subagent_run":
        return f"subagent_run over {len(arguments.get('sources', []))} source(s)"
    if tool == LOADER:
        return f"skill_load {arguments.get('name')}"
    others = [key for key in arguments if key not in {"path", "labels", "sheet"}]
    target = f" on a {Path(str(arguments['path'])).suffix.lower()} source" if isinstance(arguments.get("path"), str) else ""
    return f"{tool}{target}" + (f" ({', '.join(others)})" if others else "")


def skill_draft(run: Record, events: list[Record], name: str) -> str:
    """A SKILL.md drafted from a completed Run's events: the method, without any source or answer text."""
    policy = run.get("policy", {})
    answer = policy.get("completion_workflow") == "answer"
    requested = {event["data"]["call_id"]: event["data"] for event in events if event["kind"] == "tool.requested"}
    steps: list[tuple[str, int]] = []
    for event in events:
        if event["kind"] not in {"tool.completed", "tool.reused"}:
            continue
        call = requested.get(event["data"]["call_id"], {})
        line = _step(event["data"]["tool"], call.get("arguments") or {}, answer)
        if steps and steps[-1][0] == line:
            steps[-1] = (line, steps[-1][1] + 1)
        else:
            steps.append((line, 1))
    refusals: dict[tuple[str, str], int] = {}
    for event in events:
        if event["kind"] in {"tool.rejected", "tool.failed"}:
            key = (event["data"]["tool"], event["data"]["code"])
            refusals[key] = refusals.get(key, 0) + 1
    approved = sorted({event["data"].get("tool") or "artifact_write" for event in events if event["kind"] == "approval.approved"})
    kinds: dict[str, int] = {}
    for source in run.get("sources", []):
        suffix = Path(source["path"]).suffix.lower()
        kinds[suffix] = kinds.get(suffix, 0) + 1
    options = [label for key, label in OPTION_LABELS.items() if policy.get(key)]
    options += [f"MCP tool {tool}" for tool in policy.get("mcp_tools", [])] + [f"skill {skill}" for skill in policy.get("skills", [])]
    task = " ".join(str(run.get("task", "")).split())
    artifacts = sorted({Path(artifact["name"]).suffix.lower() for artifact in run.get("artifacts", [])})
    lines = [
        "---", f"name: {name}", "description: >", f"  Drafted from a Run. Use for tasks like: {task[:400]}", "---",
        f"# {name}", "",
        f"Drafted from Run {run['id']}. This is a draft: one Run shows what was done, not the rules behind it. "
        "Review every section, and add the decision rules and failure handling a single example cannot show.", "",
        "## When to use", task, "",
        "## Inputs",
        *[f"- {count} {suffix or 'file'} source{'s' if count > 1 else ''}" for suffix, count in sorted(kinds.items())],
        "- Options used: " + (", ".join(options) if options else "none"), "",
        "## Steps that worked",
        *[f"{number}. {line}" + (f" (×{count})" if count > 1 else "") for number, (line, count) in enumerate(steps, 1)], "",
        "## Checks that applied",
        *([f"- {tool} was sent back {'once' if count == 1 else f'{count} times'}: {code}"
           for (tool, code), count in sorted(refusals.items())] or ["- Nothing was sent back in this Run."]),
        "- Every fact needs a citation to evidence from this Run's tools; computed values cite the calculation.", "",
        "## What needed approval",
        *([f"- {tool} was approved by the user." for tool in approved]
          or ["- No approval was needed in this Run. State here what must always be approved."]), "",
        "## What to return",
        ("- A direct answer that cites evidence for every fact." if answer else
         f"- An artifact ({', '.join(artifacts) or 'text'}) with a verified evidence appendix."), "",
        "## Decision rules and failure handling", "- (add yours: what to do when a source is missing, stale or ambiguous)", "",
    ]
    return "\n".join(lines)
