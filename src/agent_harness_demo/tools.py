"""Validated, scoped tools used by every Run, independent of task wording."""

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .arithmetic_evidence import require_csv_arithmetic_evidence
from .contracts import PreparedCall, RunFailure, ToolCall, canonical_tool_arguments
from .mcp_hub import McpHub, McpTool, mcp_evidence
from .sources import MAX_SOURCE_BYTES, MAX_SOURCE_PATH, MAX_SOURCES, labelled_source_lines, query_source_labels, read_source
from .storage import Record, Store
from .tables import ratio_percent, table_result


def _table_cells(line: str) -> list[str]:
    """Split unescaped Markdown table delimiters, retaining empty cells."""
    line = line.strip()
    cells: list[str] = []
    start = 0
    for index, char in enumerate(line):
        if char != "|":
            continue
        escapes = 0
        previous = index - 1
        while previous >= 0 and line[previous] == "\\":
            escapes += 1
            previous -= 1
        if escapes % 2:
            continue
        cells.append(line[start:index])
        start = index + 1
    cells.append(line[start:])
    if line.startswith("|"):
        cells.pop(0)
    if line.endswith("|") and cells and cells[-1] == "":
        cells.pop()
    return cells


def extra_table_cell_line(content: str) -> int | None:
    """Find rows whose extra cells would be omitted by a Markdown table renderer."""
    lines = re.split(r"\r\n|\r|\n", content)
    fence: tuple[str, int] | None = None
    header: str | None = None
    width: int | None = None
    html_end: str | None = None
    for number, line in enumerate(lines, 1):
        if html_end is not None:
            if html_end in line.lower():
                html_end = None
            continue
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", line):
                fence = None
            continue
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = opening[1][0], len(opening[1])
            header, width = None, None
            continue
        if re.match(r" {0,3}<!--", line):
            if "-->" not in line:
                html_end = "-->"
            header, width = None, None
            continue
        html = re.match(r" {0,3}<(pre|script|style|textarea)(?:[\s>]|$)", line, re.IGNORECASE)
        if html:
            closing = "</" + html[1].lower() + ">"
            if closing not in line.lower():
                html_end = closing
            header, width = None, None
            continue
        if (not line.strip() or line.startswith(("    ", "\t"))
                or re.match(r" {0,3}(?:>|#{1,6}(?:[ \t]|$)|(?:[-+*]|[0-9]{1,9}[.)])[ \t])", line)):
            header, width = None, None
            continue
        cells = _table_cells(line)
        if width is not None:
            if "|" not in line:
                width, header = None, line
            elif len(cells) > width:
                return number
            continue
        if header is not None and "|" in header and all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells):
            if len(cells) == len(_table_cells(header)):
                width = len(cells)
                header = None
                continue
        header = line
    return None


def evidence_section_starts(content: str) -> list[int]:
    """Locate reserved physical headings, excluding quoted/indented/fenced examples."""
    fence: tuple[str, int] | None = None
    positions = []
    # Markdown line endings are CR/LF; Unicode separators inside ordinary
    # text must not manufacture a reserved heading.
    for physical in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", content):
        line = physical[0].rstrip("\r\n")
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", line):
                fence = None
            continue
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = opening[1][0], len(opening[1])
        elif re.fullmatch(r" {0,3}##[ \t]+Verified tool evidence(?:[ \t]+#+)?[ \t]*", line, re.IGNORECASE):
            positions.append(physical.start())
    return positions


def body_evidence_ids(content: str, *, bracketed: bool = True) -> set[str]:
    """Collect evidence IDs outside Markdown code, optionally requiring brackets."""
    fence: tuple[str, int] | None = None
    prose_lines: list[str] = []
    for line in re.split(r"\r\n|\r|\n", content):
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", line):
                fence = None
            prose_lines.append("")
            continue
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = opening[1][0], len(opening[1])
            prose_lines.append("")
            continue
        if line.startswith(("    ", "\t")):
            prose_lines.append("")
            continue
        prose_lines.append(line)
    identities: set[str] = set()
    for paragraph in re.split(r"\n[ \t]*\n", "\n".join(prose_lines)):
        runs = list(re.finditer(r"`+", paragraph))
        next_same: dict[int, int] = {}
        closing: dict[int, int] = {}
        for index in range(len(runs) - 1, -1, -1):
            width = runs[index].end() - runs[index].start()
            if width in next_same:
                closing[index] = next_same[width]
            next_same[width] = index
        pieces: list[str] = []
        cursor = index = 0
        while index < len(runs):
            end_index = closing.get(index)
            if end_index is None:
                index += 1
                continue
            pieces.append(paragraph[cursor:runs[index].start()])
            pieces.append(" ")
            cursor = runs[end_index].end()
            index = end_index + 1
        pieces.append(paragraph[cursor:])
        prose = "".join(pieces)
        parts = re.findall(r"\[([^\[\]\n]+)\]", prose) if bracketed else [prose]
        for part in parts:
            identities.update(re.findall(r"(?<![A-Za-z0-9_])(E[1-9][0-9]*)(?![A-Za-z0-9_])", part))
    return identities


def bare_referenced_evidence_ids(content: str) -> set[str]:
    """Find bare EIDs used as source references or in evidence table columns."""
    bare_prose = re.sub(r"\[[^\[\]\n]*\]", " ", content)
    prose_ids = body_evidence_ids(bare_prose, bracketed=False)
    source_refs = set(re.findall(
        r"(?i)(?:來源|source|evidence|引用|citation)\s*(?:ID|編號)?\s*[:：]?\s*(E[1-9][0-9]*)",
        bare_prose,
    ))

    lines = re.split(r"\r\n|\r|\n", content)
    prose_lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", line):
                fence = None
            prose_lines.append("")
            continue
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = opening[1][0], len(opening[1])
            prose_lines.append("")
        elif line.startswith(("    ", "\t")) or line.lstrip().startswith(">"):
            prose_lines.append("")
        else:
            prose_lines.append(line)

    table_refs: set[str] = set()
    for index in range(len(prose_lines) - 1):
        header = _table_cells(prose_lines[index])
        separator = _table_cells(prose_lines[index + 1])
        if (len(header) < 2 or len(header) != len(separator)
                or not all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in separator)):
            continue
        evidence_columns = [column for column, label in enumerate(header) if re.search(
            r"(?:證據|evidence)\s*(?:id|編號)?|(?:來源|source|引用|citation)\s*(?:id|編號)?",
            label,
            re.IGNORECASE,
        )]
        if not evidence_columns:
            continue
        for row in prose_lines[index + 2:]:
            cells = _table_cells(row)
            if len(cells) != len(header) or "|" not in row:
                break
            for column in evidence_columns:
                cell = re.sub(r"\[[^\[\]\n]*\]", " ", cells[column])
                table_refs.update(body_evidence_ids(cell, bracketed=False))
    return source_refs | (table_refs & prose_ids)

# Accommodate every admitted source plus bounded UTF-8 path headings and the fixture notice.
MAX_ARTIFACT_BYTES = MAX_SOURCES * MAX_SOURCE_BYTES + 32_768
PATH_SCHEMA = {"type": "string", "minLength": 1, "maxLength": MAX_SOURCE_PATH}
EVIDENCE_SCHEMA = {"type": "string", "pattern": "^E[1-9][0-9]*$"}

DEFINITIONS: dict[str, Record] = {
    "plan_update": {
        "effect": "run_metadata",
        "description": "Save/revise the public action plan before substantive work. Before inspecting sources, list actions and their order only. Do not guess field names, column names, values, or missing items before inspecting sources. After inspection, tie findings to returned evidence. No private reasoning.",
        "parameters": {"type": "object", "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 200}}}, "required": ["steps"], "additionalProperties": False},
    },
    "table_inspect": {
        "effect": "read_only",
        "description": "Inspect CSV headers, rows, sample, full-column aggregatable_columns, hash and lines. Availability is not a computed value: use table_aggregate even if all rows fit.",
        "parameters": {"type": "object", "properties": {"path": PATH_SCHEMA}, "required": ["path"], "additionalProperties": False},
    },
    "table_aggregate": {
        "effect": "read_only",
        "description": "Sum/mean a full numeric CSV column; return numeric evidence_id for calculation/citation.",
        "parameters": {"type": "object", "properties": {"path": PATH_SCHEMA,
            "column": {"type": "string", "minLength": 1, "maxLength": 100}, "operation": {"enum": ["sum", "mean"]}},
            "required": ["path", "column", "operation"], "additionalProperties": False},
    },
    "number_calculate": {
        "effect": "read_only",
        "description": "ratio_percent: numerator / denominator * 100. Operands must be prior numeric evidence IDs, never literals.",
        "parameters": {"type": "object", "properties": {"operation": {"type": "string", "enum": ["ratio_percent"]},
            "numerator": EVIDENCE_SCHEMA, "denominator": EVIDENCE_SCHEMA},
            "required": ["operation", "numerator", "denominator"], "additionalProperties": False},
    },
    "workspace_list": {
        "effect": "read_only",
        "description": "List this task's selected sources.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "source_read": {
        "effect": "read_only",
        "description": "Read UTF-8/hash. labels=[] reads raw; otherwise query exact case-sensitive colon labels. complete=false means unavailable, not absent. Not a semantic schema.",
        "parameters": {"type": "object", "properties": {"path": PATH_SCHEMA,
            "labels": {"type": "array", "minItems": 0, "maxItems": 8, "uniqueItems": True,
                       "items": {"type": "string", "minLength": 1, "maxLength": 100}}},
                       "required": ["path"], "additionalProperties": False},
    },
    "base_artifact_read": {
        "effect": "read_only",
        "description": "Read pinned artifact as fresh version evidence, not new calculations. Cite returned EID for preserved claims; historical EIDs are not current.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "artifact_write": {
        "effect": "immutable_artifact",
        "description": "Publish immutable Markdown/text for this Run. Omit copied evidence sections; server supplies the current appendix.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"^[^/\\:\x00-\x1f]+$"},
            "content": {"type": "string", "minLength": 1, "maxLength": MAX_ARTIFACT_BYTES},
            "citations": {"type": "array", "maxItems": 20, "uniqueItems": True, "items": EVIDENCE_SCHEMA},
        }, "required": ["name", "content"], "additionalProperties": False},
    },
    "task_finish": {
        "effect": "run_metadata",
        "description": "Declare completion after confirmed publication.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 512, "pattern": r"\S"}},
            "required": ["summary"], "additionalProperties": False},
    },
}
ARTIFACT_TOOLS = {"artifact_write", "artifact_draft"}
ARTIFACT_RECEIPT_PARAMETERS: Record = {
    "type": "object", "properties": {"artifact_ids": {"type": "array", "minItems": 1, "maxItems": 20,
        "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}},
    "required": ["artifact_ids"], "additionalProperties": False,
}
DEFINITIONS["artifact_draft"] = {
    **deepcopy(DEFINITIONS["artifact_write"]), "effect": "run_metadata",
    "description": "Stage a candidate, not an artifact. Omit copied evidence sections; server supplies the current appendix. Review against task and sources before publishing later.",
}
DRAFT_PUBLICATION_PARAMETERS: Record = {
    "type": "object", "properties": {
        "name": deepcopy(DEFINITIONS["artifact_write"]["parameters"]["properties"]["name"]),
        "draft_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "citations": deepcopy(DEFINITIONS["artifact_write"]["parameters"]["properties"]["citations"]),
    }, "required": ["name", "draft_sha256", "citations"], "additionalProperties": False,
}


def model_tools(*, require_citations: bool = False, include_tables: bool = True, include_labels: bool = True,
                include_finish: bool = False, include_draft: bool = False,
                receipt_completion: bool = False, include_base: bool = False,
                aggregate_operations: tuple[str, ...] = ("sum", "mean"),
                extra_tools: list[Record] | None = None) -> list[Record]:
    definitions = deepcopy(DEFINITIONS)
    if not include_base:
        del definitions["base_artifact_read"]
    if not include_draft:
        del definitions["artifact_draft"]
    else:
        definitions["artifact_write"]["parameters"] = deepcopy(DRAFT_PUBLICATION_PARAMETERS)
        definitions["artifact_write"]["description"] = "Publish latest draft SHA256; same citations. Edits need artifact_draft."
    if not include_finish:
        del definitions["task_finish"]
    elif receipt_completion:
        definitions["task_finish"]["parameters"] = deepcopy(ARTIFACT_RECEIPT_PARAMETERS)
        definitions["task_finish"]["description"] = "If task met, select published artifact IDs for a verified delivery receipt. No factual summary."
    if not include_labels:
        del definitions["source_read"]["parameters"]["properties"]["labels"]
        definitions["source_read"]["description"] = "Read a selected UTF-8 source with hash."
    else:
        definitions["source_read"]["parameters"]["required"].append("labels")
    if not include_tables:
        for name in ("table_inspect", "table_aggregate", "number_calculate"):
            del definitions[name]
    else:
        definitions["table_aggregate"]["parameters"]["properties"]["operation"]["enum"] = list(
            aggregate_operations
        )
    if require_citations:
        for name in ARTIFACT_TOOLS & definitions.keys():
            parameters = definitions[name]["parameters"]
            parameters["properties"]["name"]["pattern"] = r"^(?!\.)[^/\\:\x00-\x1f]+\.(?:[mM][dD]|[tT][xX][tT])$"
            parameters["properties"]["name"]["description"] = "Filename ending in .md or .txt."
            if "citations" not in parameters["required"]:
                parameters["required"].append("citations")
            parameters["properties"]["citations"]["minItems"] = 1
    return [{"type": "function", "function": {"name": name, "description": definition["description"],
             "parameters": definition["parameters"]}} for name, definition in definitions.items()] + deepcopy(extra_tools or [])


class Tools:
    def __init__(self, store: Store, run_id: str, root: Path, sources: list[Record], *, require_citations: bool = False,
                 fault_once_source_read: bool = False, receipt_completion: bool = False,
                 base_artifact: Record | None = None, mcp_tools: dict[str, McpTool] | None = None,
                 mcp_hub: McpHub | None = None) -> None:
        self.store = store
        self.mcp_tools = dict(mcp_tools or {})
        self.mcp_hub = mcp_hub
        self.run_id = run_id
        self.receipt_completion = receipt_completion
        self.root = root
        self.sources = {source["path"]: source for source in sources}
        self.evidence: dict[str, Record] = {}
        self.require_citations = require_citations
        self.base_artifact = deepcopy(base_artifact)
        self._fault_pending = fault_once_source_read
        self.actions: dict[str, Callable[[Record], Awaitable[Record]]] = {
            "plan_update": self._plan,
            "table_inspect": self._inspect,
            "table_aggregate": self._aggregate,
            "number_calculate": self._calculate,
            "workspace_list": self._list,
            "source_read": self._read,
            "base_artifact_read": self._read_base,
            "task_finish": self._finish,
            "artifact_draft": self._draft,
        }

    def restore_evidence(self, confirmed: list[Record]) -> None:
        for call in confirmed:
            result = call["result"]
            if "evidence_id" not in result:
                continue
            if call["tool"] in self.mcp_tools:
                expected = mcp_evidence(self.mcp_tools[call["tool"]], result.get("arguments", {}), result.get("text", ""))
                if {key: value for key, value in result.items() if key != "evidence_id"} != expected:
                    raise RunFailure("checkpoint_evidence_invalid", "Restored MCP evidence must match its recorded text and tool.")
            elif call["tool"] not in {"source_read", "table_inspect", "table_aggregate", "number_calculate", "base_artifact_read"}:
                raise RunFailure("checkpoint_evidence_invalid", "The checkpoint contains an unsupported evidence producer.")
            elif call["tool"] == "base_artifact_read":
                expected = self._base_result()
                if {key: value for key, value in result.items() if key != "evidence_id"} != expected:
                    raise RunFailure("checkpoint_evidence_invalid", "Restored artifact evidence must match the pinned base version.")
            elif "path" in result:
                source = self.sources.get(result["path"])
                if source is None or source["sha256"] != result["sha256"]:
                    raise RunFailure("checkpoint_evidence_invalid", "Restored evidence must match selected source versions.")
            elif any(identity not in self.evidence for identity in result.get("inputs", [])):
                raise RunFailure("checkpoint_evidence_invalid", "Restored calculation evidence is missing its source inputs.")
            identity = result["evidence_id"]
            if identity in self.evidence and self.evidence[identity] != result:
                raise RunFailure("checkpoint_evidence_invalid", "The checkpoint contains conflicting evidence identities.")
            self.evidence[identity] = deepcopy(result)

    def _citation_hint(self) -> str:
        current = {}
        for identity, evidence in self.evidence.items():
            label = evidence.get("name") if evidence.get("evidence_type") == "pinned_artifact" else evidence.get("path")
            kind = "pinned_artifact" if evidence.get("evidence_type") == "pinned_artifact" else "calculation" if "value" in evidence else "source"
            current[identity] = {"kind": kind, "cite": f"[{label} {identity}]" if label else f"[{identity}]"}
        return (" Available evidence IDs: " + json.dumps(list(self.evidence))
                + ". source_ref metadata creates no new evidence IDs; cite confirmed evidence keys."
                + " Current body citation forms: " + json.dumps(current, ensure_ascii=False, separators=(",", ":"))
                + ". Respect correctable_fields for this retry; other body edits need a later separate decision using offered tools after the error is resolved. "
                + "pinned_artifact refers to the snapshot, not its historical EIDs or fresh source calculations.")

    def _invalid_numeric_fields(self, arguments: Record) -> tuple[str, ...]:
        fields = []
        for field in ("numerator", "denominator"):
            reference = arguments.get(field)
            evidence = self.evidence.get(reference) if isinstance(reference, str) else None
            if evidence is None or "value" not in evidence:
                fields.append(field)
        return tuple(fields)

    def _numeric_hint(self, arguments: Record | None = None) -> str:
        available = {identity: {key: result[key] for key in ("path", "column", "operation", "inputs", "value") if key in result}
                     for identity, result in self.evidence.items() if "value" in result}
        hint = (" Use numeric evidence IDs, not literal numbers. Available numeric evidence: "
                + json.dumps(available, ensure_ascii=False, separators=(",", ":")))
        remap: dict[str, str] = {}
        if arguments is not None:
            for field in self._invalid_numeric_fields(arguments):
                literal = arguments.get(field)
                if not isinstance(literal, str):
                    continue
                matches = [identity for identity, result in self.evidence.items()
                           if "value" in result and str(result["value"]) == literal]
                if len(matches) == 1:
                    remap[field] = matches[0]
            if len(remap) > 1 and len(set(remap.values())) != len(remap):
                remap = {}
        if remap:
            hint += ". Operand remap: " + json.dumps(remap, ensure_ascii=False, separators=(",", ":"))
        return hint

    def validate(self, call: ToolCall) -> None:
        if call.name in self.mcp_tools:
            try:
                Draft202012Validator(self.mcp_tools[call.name].input_schema).validate(call.arguments)
            except ValidationError as error:
                mcp_fields = tuple(str(part) for part in list(error.absolute_path)[:1]) or tuple(call.arguments)
                raise RunFailure("invalid_arguments", f"Invalid MCP tool input at {error.json_path}: {error.message[:200]}",
                                 correctable_fields=mcp_fields) from error
            return
        if call.name not in DEFINITIONS or (call.name == "base_artifact_read" and self.base_artifact is None):
            raise RunFailure("unknown_tool", "The requested tool is not available to this Run.")
        try:
            parameters = (ARTIFACT_RECEIPT_PARAMETERS if call.name == "task_finish" and self.receipt_completion
                          else DEFINITIONS[call.name]["parameters"])
            Draft202012Validator(parameters).validate(call.arguments)
        except ValidationError as error:
            hint = f" Allowed values: {error.validator_value!r}." if error.validator in {"enum", "const"} else ""
            path = list(error.absolute_path)
            if call.name in ARTIFACT_TOOLS and path[:1] == ["citations"] and error.validator == "pattern":
                hint = self._citation_hint()
            fields: tuple[str, ...] = ()
            if path and isinstance(path[0], str):
                fields = (path[0],)
            elif error.validator == "required" and isinstance(error.validator_value, list):
                fields = tuple(field for field in error.validator_value if isinstance(field, str) and field not in call.arguments)
            elif error.validator == "additionalProperties":
                properties = parameters["properties"]
                fields = tuple(field for field in call.arguments if field not in properties)
            if call.name == "number_calculate" and any(field in fields for field in ("numerator", "denominator")):
                fields = self._invalid_numeric_fields(call.arguments)
                hint = self._numeric_hint(call.arguments)
            raise RunFailure("invalid_arguments", f"Invalid tool input at {error.json_path}: {error.validator} constraint.{hint}", correctable_fields=fields) from error
        if call.name in {"source_read", "table_inspect", "table_aggregate"} and call.arguments["path"] not in self.sources:
            raise RunFailure("source_forbidden", "The requested source was not selected for this task.")
        if call.name in {"table_inspect", "table_aggregate"} and Path(call.arguments["path"]).suffix.lower() != ".csv":
            raise RunFailure("invalid_arguments", "Table tools require a selected CSV file.")
        references = call.arguments.get("citations", []) if call.name in ARTIFACT_TOOLS else []
        if call.name == "number_calculate":
            references = [call.arguments["numerator"], call.arguments["denominator"]]
        if any(reference not in self.evidence for reference in references):
            fields = ("citations",) if call.name in ARTIFACT_TOOLS else tuple(
                field for field in ("numerator", "denominator") if call.arguments[field] not in self.evidence)
            hint = self._citation_hint() if call.name in ARTIFACT_TOOLS else ""
            if call.name == "number_calculate":
                fields = self._invalid_numeric_fields(call.arguments)
                hint = self._numeric_hint(call.arguments)
            raise RunFailure("invalid_evidence", "Only evidence produced by this Run can be referenced." + hint, correctable_fields=fields)
        if call.name == "number_calculate" and any("value" not in self.evidence[ref] for ref in references):
            raise RunFailure("invalid_evidence", "Calculation operands must reference numeric evidence." + self._numeric_hint(call.arguments),
                             correctable_fields=self._invalid_numeric_fields(call.arguments))
        if call.name in ARTIFACT_TOOLS:
            if self.require_citations and not references:
                raise RunFailure("missing_evidence", "Local model artifacts require at least one citation to evidence produced by this Run.", correctable_fields=("citations",))
            name = call.arguments["name"]
            if name.startswith(".") or Path(name).suffix.lower() not in {".md", ".txt"}:
                raise RunFailure("invalid_arguments", "Use a plain filename ending in .md or .txt.", correctable_fields=("name",))
            if Path(name).suffix.lower() == ".md":
                line = extra_table_cell_line(call.arguments["content"])
                if line is not None:
                    raise RunFailure("invalid_arguments", f"Markdown table row at line {line} has more cells than its header; align the table columns.", correctable_fields=("content",))
            if len(call.arguments["content"].encode("utf-8")) > MAX_ARTIFACT_BYTES:
                raise RunFailure("invalid_arguments", "Artifact content exceeds the bounded snapshot capacity.", correctable_fields=("content",))

    def prepare(self, call: ToolCall, *, draft: Record | None = None,
                completion_artifacts: list[Record] | None = None) -> PreparedCall:
        from_draft = call.name == "artifact_write" and "draft_sha256" in call.arguments
        if from_draft:
            if (draft is None or set(call.arguments) != {"name", "draft_sha256", "citations"}
                    or call.arguments["name"] != draft["name"]
                    or call.arguments["draft_sha256"] != hashlib.sha256(draft["content"].encode("utf-8")).hexdigest()):
                raise RunFailure("unreviewed_artifact", "The reference must select the latest staged candidate without edits.")
            call = ToolCall(call.identity, call.name, {"name": draft["name"], "content": draft["content"],
                                                      "citations": deepcopy(call.arguments["citations"])})
        self.validate(call)
        try:
            arguments = (self._artifact_arguments(
                call.arguments, reconcile_body_citations=call.name == "artifact_write" and not from_draft,
            )
                         if call.name in ARTIFACT_TOOLS else canonical_tool_arguments(call.name, call.arguments))
        except RunFailure as error:
            if from_draft and error.code in {"evidence_section_mismatch", "missing_body_citation", "invalid_evidence"}:
                raise RunFailure("unreviewed_artifact", "A draft SHA must retain its exact current evidence bindings. Stage a new candidate for edits.") from error
            raise
        scope = {"workspace": str(self.root),
            "artifact_directory": str(self.store.directory / "artifacts"),
            "effect": ((("external_effect" if self.mcp_tools[call.name].approval else "read_only"))
                       if call.name in self.mcp_tools else DEFINITIONS[call.name]["effect"]),
            "sources": deepcopy(list(self.sources.values()))}
        if call.name in self.mcp_tools:
            scope["mcp"] = {"server": self.mcp_tools[call.name].server, "tool": self.mcp_tools[call.name].tool}
            if self.mcp_tools[call.name].approval:
                scope["approval"] = "required"  # bound into the digest that the single-use approval covers
        if call.name == "base_artifact_read":
            assert self.base_artifact is not None
            scope["base_artifact"] = {key: self.base_artifact[key] for key in ("id", "run_id", "sha256", "version")}
        if call.name == "task_finish" and self.receipt_completion:
            available = {artifact["id"]: artifact for artifact in completion_artifacts or []}
            if any(identity not in available for identity in arguments["artifact_ids"]):
                raise RunFailure("invalid_completion", "Receipt IDs must select confirmed artifacts of this Run or its inherited checkpoint.", correctable_fields=("artifact_ids",))
            scope["completion_receipts"] = [{key: available[identity][key] for key in ("id", "name", "sha256")}
                                            for identity in arguments["artifact_ids"]]
        return PreparedCall(call, arguments, scope)

    async def execute(self, prepared: PreparedCall) -> Record:
        call = prepared.call
        if call.name == "source_read" and self._fault_pending:
            self._fault_pending = False
            await self.store.record(self.run_id, "tool.fault_injected", {
                "call_id": call.identity, "tool": call.name, "kind": "controlled_read_failure", "before_filesystem_access": True,
            })
            raise RunFailure("source_unavailable", "Controlled validation fault: this read failed before filesystem access. Repeat source_read with the same arguments in a new decision to resolve this error.")
        if call.name == "artifact_write":
            return await self.store.publish(self.run_id, prepared.arguments["name"], prepared.arguments["content"],
                                            call_id=call.identity)
        if call.name == "task_finish" and self.receipt_completion:
            return {"summary": "已發布：" + "、".join(item["name"] for item in prepared.scope["completion_receipts"]) + "。",
                    "artifact_ids": deepcopy(prepared.arguments["artifact_ids"]), "receipt": True}
        if call.name in self.mcp_tools:
            if self.mcp_hub is None:
                raise RunFailure("mcp_unavailable", "No MCP servers are connected.")
            return self._evidence(await self.mcp_hub.call(self.mcp_tools[call.name], prepared.arguments))
        return await self.actions[call.name](prepared.arguments)

    async def _list(self, arguments: Record) -> Record:
        return {"sources": list(self.sources.values())}

    async def _draft(self, arguments: Record) -> Record:
        return {**arguments, "sha256": hashlib.sha256(arguments["content"].encode("utf-8")).hexdigest(), "published": False}

    async def _read(self, arguments: Record) -> Record:
        source = await self._source(arguments["path"])
        labelled = await asyncio.to_thread(labelled_source_lines, source["text"])
        if labelled:
            source["labelled_lines"] = labelled
        if arguments.get("labels"):
            source["label_queries"] = await asyncio.to_thread(query_source_labels, source["text"], arguments["labels"])
        return self._evidence({**source, "line_start": 1, "line_end": max(1, len(source["text"].splitlines()))})

    def _base_result(self) -> Record:
        base = self.base_artifact
        if base is None:
            raise RunFailure("base_artifact_unavailable", "No base artifact was selected for this task.")
        if hashlib.sha256(base["text"].encode("utf-8")).hexdigest() != base["sha256"]:
            raise RunFailure("base_artifact_unavailable", "The pinned base snapshot does not match its hash.")
        return {"evidence_type": "pinned_artifact", "artifact_id": base["id"], "origin_run_id": base["run_id"],
                "name": base["name"], "version": base["version"], "sha256": base["sha256"], "text": base["text"],
                "line_start": 1, "line_end": max(1, len(base["text"].splitlines()))}

    async def _read_base(self, arguments: Record) -> Record:
        return self._evidence(self._base_result())

    async def _source(self, path: str) -> Record:
        source = await asyncio.to_thread(read_source, self.root, path)
        if source["sha256"] != self.sources[path]["sha256"]:
            raise RunFailure("source_changed", "The source changed after task admission. Start a new task with its current version.")
        return source

    def _evidence(self, result: Record) -> Record:
        identity = f"E{len(self.evidence) + 1}"
        result["evidence_id"] = identity
        self.evidence[identity] = result
        return result

    async def _plan(self, arguments: Record) -> Record:
        await self.store.record(self.run_id, "plan.updated", arguments)
        return {"saved": True, **arguments}

    async def _finish(self, arguments: Record) -> Record:
        return {"summary": arguments["summary"]}

    async def _inspect(self, arguments: Record) -> Record:
        source = await self._source(arguments["path"])
        return self._evidence(await asyncio.to_thread(table_result, source))

    async def _aggregate(self, arguments: Record) -> Record:
        source = await self._source(arguments["path"])
        return self._evidence(await asyncio.to_thread(table_result, source, arguments["column"], arguments["operation"]))

    async def _calculate(self, arguments: Record) -> Record:
        refs = [arguments["numerator"], arguments["denominator"]]
        return self._evidence({"operation": "ratio_percent", "inputs": refs,
            "value": ratio_percent(self.evidence[refs[0]]["value"], self.evidence[refs[1]]["value"])})

    def _evidence_sources(self, identity: str) -> set[tuple[str, str, str]]:
        sources: set[tuple[str, str, str]] = set()
        pending = [identity]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            evidence = self.evidence[current]
            if evidence.get("evidence_type") == "pinned_artifact":
                sources.add(("artifact", evidence["artifact_id"], evidence["sha256"]))
            elif "path" in evidence:
                sources.add(("source", evidence["path"], evidence["sha256"]))
            else:
                pending.extend(evidence["inputs"])
        return sources

    def _require_body_source_links(self, body: str, references: set[str]) -> None:
        roots = {identity: self._evidence_sources(identity) for identity in references}
        expected = set().union(*roots.values())
        if len(expected) < 2:
            return
        mentioned = body_evidence_ids(body, bracketed=False)
        covered = set().union(*(sources for identity, sources in roots.items() if identity in mentioned))
        if expected - covered:
            raise RunFailure("missing_body_citation",
                "Multiple source versions are cited, but the body does not reference each source through its current evidence IDs. "
                "Add the appropriate IDs next to supported body statements; a global server appendix alone does not link entries to sources. "
                "Missing source versions: " + json.dumps(sorted(expected - covered), ensure_ascii=False)
                + self._citation_hint(), correctable_fields=("content",))

    def _artifact_arguments(self, arguments: Record, *, reconcile_body_citations: bool = True) -> Record:
        content = arguments["content"]
        references = list(arguments.get("citations", []))
        # The body is the user-visible citation record. If it already names
        # same-Run evidence, reconcile that verified set with the redundant
        # citations metadata before constructing the server-owned appendix.
        # This avoids spending model correction turns on a metadata-only
        # omission while preserving rejection of unknown EIDs below.
        if reconcile_body_citations:
            starts = evidence_section_starts(content)
            body_preview = content[:starts[0]] if starts else content
            preview_ids = body_evidence_ids(body_preview)
            preview_sources = set(references) | (preview_ids & self.evidence.keys())
            preview_source_lines = {
                line for identity in preview_sources
                for line in self.evidence[identity].get("text", "").splitlines() if line
            }
            citation_preview = "\n".join(
                "" if line in preview_source_lines and "[" in line else line
                for line in re.split(r"\r\n|\r|\n", body_preview)
            )
            body_reference_ids = body_evidence_ids(citation_preview)
            unknown_preview_ids = body_reference_ids - self.evidence.keys()
            if unknown_preview_ids:
                raise RunFailure(
                    "invalid_evidence", "Body evidence IDs do not exist in this Run: "
                    + json.dumps(sorted(unknown_preview_ids)) + self._citation_hint(), correctable_fields=("content",),
                )
            references.extend(sorted(body_reference_ids - set(references)))
        if references:
            notes = ["\n\n## Verified tool evidence"]
            visited: set[str] = set()
            while references:
                identity = references.pop(0)
                if identity in visited:
                    continue
                visited.add(identity)
                evidence = self.evidence[identity]
                if evidence.get("evidence_type") == "pinned_artifact":
                    notes.append(f"- [{identity}] artifact {evidence['artifact_id']} ({evidence['name']}, version {evidence['version']}, Run {evidence['origin_run_id']}):L{evidence['line_start']}-L{evidence['line_end']} SHA-256 {evidence['sha256']}; artifact snapshot, not a fresh source calculation")
                elif "path" in evidence:
                    notes.append(f"- [{identity}] {evidence['path']}:L{evidence['line_start']}-L{evidence['line_end']} SHA-256 {evidence['sha256']}"
                                 + (f"; {evidence['operation']}({evidence['column']}) = {evidence['value']}" if "value" in evidence else ""))
                else:
                    references.extend(evidence["inputs"])
                    notes.append(f"- [{identity}] {evidence['operation']}({', '.join(evidence['inputs'])}) = {evidence['value']}")
            require_csv_arithmetic_evidence(content, [self.evidence[identity] for identity in visited])
            appendix = "\n".join(notes) + "\n"
            exact_suffix = content.rstrip("\r\n").endswith(appendix.rstrip("\r\n"))
            proposed = content if exact_suffix else content + appendix
            canonical_start = len(proposed.rstrip("\r\n")) - len(appendix.rstrip("\r\n")) + 2
            if evidence_section_starts(proposed) != [canonical_start]:
                raise RunFailure("evidence_section_mismatch",
                    "Verified tool evidence is a server-owned appendix for this Run. Remove the copied section from content; "
                    "close literal code fences, keep supported claims and cite current evidence IDs. The server appends the verified section."
                    + self._citation_hint(), correctable_fields=("content",))
            body = proposed[:canonical_start - 2]
            source_lines = {line for identity in visited for line in self.evidence[identity].get("text", "").splitlines() if line}
            # Exact source lines may contain literal labels that resemble EIDs.
            # Keep line boundaries so code/paragraph parsing cannot span removed lines.
            citation_body = "\n".join("" if line in source_lines and "[" in line else line
                                      for line in re.split(r"\r\n|\r|\n", body))
            body_ids = body_evidence_ids(body)
            unknown = body_evidence_ids(citation_body) - self.evidence.keys()
            if unknown:
                raise RunFailure("invalid_evidence", "Body evidence IDs do not exist in this Run: "
                                 + json.dumps(sorted(unknown)) + self._citation_hint(), correctable_fields=("content",))
            bracketed_body_ids = body_evidence_ids(citation_body)
            reference_body = "\n".join(
                "" if line in source_lines else line for line in re.split(r"\r\n|\r|\n", body)
            )
            bare_body_ids = bare_referenced_evidence_ids(reference_body) - bracketed_body_ids
            unknown_bare_ids = bare_body_ids - self.evidence.keys()
            if unknown_bare_ids:
                raise RunFailure(
                    "invalid_evidence",
                    "Body refers to unknown evidence IDs: " + json.dumps(sorted(unknown_bare_ids)) + self._citation_hint(),
                    correctable_fields=("content",),
                )
            bare_body_ids &= self.evidence.keys()
            if bare_body_ids:
                citation_forms = {}
                for identity in sorted(bare_body_ids):
                    evidence = self.evidence[identity]
                    label = evidence.get("name") if evidence.get("evidence_type") == "pinned_artifact" else evidence.get("path")
                    citation_forms[identity] = f"[{label} {identity}]" if label else f"[{identity}]"
                raise RunFailure(
                    "missing_body_citation",
                    "正文或證據表格中的裸 EID 不算引用，請改用："
                    + json.dumps(citation_forms, ensure_ascii=False, separators=(",", ":")),
                    correctable_fields=("content",),
                )
            unbound = (body_ids & self.evidence.keys()) - visited
            if unbound:
                raise RunFailure("invalid_evidence", "Body evidence IDs must be included in the declared citations or their verified inputs: "
                                 + json.dumps(sorted(unbound)) + self._citation_hint(), correctable_fields=("citations",))
            self._require_body_source_links(proposed[:canonical_start - 2], visited)
            # A revision may already contain this exact, freshly validated
            # evidence appendix. Keep the proposed bytes; do not append it twice.
            content = proposed
        if len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise RunFailure("invalid_arguments", "Artifact and source citations exceed the size limit.", correctable_fields=("content",))
        return {"name": arguments["name"], "content": content}
