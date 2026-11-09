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
