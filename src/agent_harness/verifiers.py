"""Generic artifact verifiers.

A verifier sees only the candidate artifact content and the server-held tool
evidence. It never receives the user's task text, so it cannot encode answers
for particular tasks; it can only check that what the model wrote is backed by
what the tools actually returned.
"""

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
import re

from .contracts import RunFailure
from .storage import Record
from .tools import body_evidence_ids, evidence_section_starts

Verifier = Callable[[str, dict[str, Record]], RunFailure | None]

CALCULATION_OPERATIONS = frozenset({"sum", "mean", "ratio_percent"})
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(?:[+-]?(?:\d{1,3}(?:,\d{3})+|\d+|\.\d+)(?:\.\d+)?)(?:%)?")


def artifact_body(content: str) -> str:
    """Model-written body, excluding the server-generated evidence appendix.

    Uses the same detector as publication, which ignores fenced, quoted or
    indented copies of the heading, so a copy inside the body cannot hide the
    text after it from the verifiers.
    """
    starts = evidence_section_starts(content)
    return content[:starts[0]] if starts else content


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "").rstrip("%"))
    except InvalidOperation:
        return None


def _source_path(identity: str, evidence: dict[str, Record], seen: frozenset[str] = frozenset()) -> str | None:
    if identity in seen:
        return None
    record = evidence.get(identity, {})
    path = record.get("path")
    if isinstance(path, str):
        return path
    for parent in record.get("inputs", []):
        if isinstance(parent, str) and (found := _source_path(parent, evidence, seen | {identity})):
            return found
    return None


def calculation_citations(content: str, evidence: dict[str, Record]) -> RunFailure | None:
    """A computed value must not be attributed to evidence that did not produce it.

    A line that cites evidence next to a computed value must include the
    calculation that produced it, whatever the number of sources. A line with
    no inline citation is only rejected when evidence spans several sources:
    with a single source the server appendix already links every value, which
    matches the body-link rule for artifacts.
    """
    calculations = {
        identity: record for identity, record in evidence.items()
        if record.get("operation") in CALCULATION_OPERATIONS and isinstance(record.get("value"), str)
    }
    expected = {identity: _decimal(str(record["value"])) for identity, record in calculations.items()}
    multiple_sources = len({record["path"] for record in evidence.values()
                            if isinstance(record.get("path"), str)}) > 1
    issues: list[str] = []
    for line in artifact_body(content).splitlines():
        stated = {value for match in _NUMBER.finditer(line) if (value := _decimal(match.group())) is not None}
        cited = body_evidence_ids(line)
        if not cited and not multiple_sources:
            continue
        uncited: dict[Decimal, list[str]] = {}
        for identity, value in expected.items():
            if value is not None and value in stated:
                uncited.setdefault(value, []).append(identity)
        missing = {value: ids for value, ids in uncited.items() if not cited.intersection(ids)}
        if not missing:
            continue
        references = "；".join(
            f"{value}：" + " 或 ".join(
                f"[{path} {identity}]" if (path := _source_path(identity, evidence)) else f"[{identity}]"
                for identity in sorted(ids)
            )
            for value, ids in missing.items()
        )
        issues.append(f"「{line.strip()[:120]}」缺少計算引用：{references}")
    if not issues:
        return None
    return RunFailure(
        "missing_calculation_citation",
        "正文中的計算值須在同一行引用產生它的計算證據；請補上引用，或刪除重複的數值。" + " ".join(issues[:6]),
        correctable_fields=("content", "citations"),
    )


_ANNOTATION = re.compile(r"\s*[(（][^()（）]*[)）]")
_VALUE_EDGE = re.compile(r"^[\s*_`\"'「」]+|[\s*_`\"'「」.,;:，。；：]+$")


def _plain(text: str) -> str:
    return _VALUE_EDGE.sub("", text).casefold()


def _labelled_artifact_values(body: str, is_source_value: Callable[[str], bool]) -> list[tuple[str, str]]:
    """(output label, value) pairs from 'label: value' lines and Markdown tables.

    A table where some row holds two or more source values describes one
    entity per row (for example one document), so cells are labelled by their
    column headers. A table whose rows each hold at most one source value is a
    key/value table: each row's first cell is the label for its other cells.
    """
    pairs: list[tuple[str, str]] = []
    lines = body.splitlines()
    separator = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?")

    def flush(headers: list[str], rows: list[list[str]]) -> None:
        entity_table = any(sum(is_source_value(cell) for cell in row) > 1 for row in rows)
        for row in rows:
            if entity_table:
                pairs.extend((header, cell) for header, cell in zip(headers, row) if header)
            else:
                pairs.extend((_label(row[0]), cell) for cell in row[1:])

    headers: list[str] | None = None
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if headers is None and separator.fullmatch(following):
                headers, rows = [_label(cell) for cell in cells], []
            elif headers is not None and not separator.fullmatch(stripped):
                rows.append(cells)
            continue
        if headers is not None:
            flush(headers, rows)
            headers = None
        match = re.match(r"^(?:[-*+]|\d+[.)])?\s*(.+?)\s*[:：]\s*(.+)$", stripped)
        if match:
            pairs.append((_label(match[1]), match[2]))
    if headers is not None:
        flush(headers, rows)
    return pairs


def consistent_source_roles(content: str, evidence: dict[str, Record]) -> RunFailure | None:
    """One output label must not hold values that the sources give under different labels.

    For each artifact value that matches a labelled source value, the source
    label is that value's role. If one output label collects values from two
    different source roles, a value was probably moved into another role's
    slot (for example a contact filled in as the owner). A value whose line
    restates its source label keeps its role visible and is not counted.
    Only the artifact and tool evidence are read, never the task text.
    """
    roles: dict[str, list[tuple[str, str, str]]] = {}
    labels_by_path: dict[str, dict[str, str]] = {}
    for record in evidence.values():
        path = record.get("path")
        for item in record.get("labelled_lines") or []:
            if isinstance(path, str) and isinstance(item, dict) and _plain(str(item.get("value", ""))):
                value = str(item["value"]).strip()
                label = str(item.get("label", ""))
                roles.setdefault(_plain(value), []).append((label, path, value))
                labels_by_path.setdefault(path, {}).setdefault(label, value)
    if not roles:
        return None
    # output field -> source role -> [(value as written in the source, source path)], in artifact order
    by_output: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for labelled, value in _labelled_artifact_values(artifact_body(content), lambda cell: _plain(cell) in roles):
        sources = roles.get(_plain(value))
        if not sources or len({label for label, _, _ in sources}) != 1:
            continue
        source_label, path, original = sources[0]
        # "負責人 (Owner)" and "負責人 (Incident escalation contact)" are one field to
        # a reader; a role annotation in the label does not move the value out of it.
        output_label = _ANNOTATION.sub("", labelled).strip() or labelled
        if source_label.casefold() in value.casefold() or source_label.casefold() == output_label.casefold():
            continue
        by_output.setdefault(output_label, {}).setdefault(source_label, []).append((original, path))

    def reading(output_label: str, role: str, source_roles: dict[str, list[tuple[str, str]]]) -> str:
        """What to change if this field means `role`, derived only from the sources."""
        steps: list[str] = []
        for other_role, entries in source_roles.items():
            if other_role == role:
                continue
            for value, path in entries:
                own = labels_by_path.get(path, {}).get(role)
                fix = f"{path} 的 {role} 是 {own}，請改為 {own}" if own else f"{path} 沒有 {role}，請改為未提供"
                steps.append(f"{fix}（{value} 是 {path} 的 {other_role}，請放在對應欄位或寫明這個角色）")
        return f"若「{output_label}」是 {role}：" + "；".join(steps)

    issues: list[str] = []
    for output_label, source_roles in by_output.items():
        if len(source_roles) < 2:
            continue
        found = "、".join(f"{value}（{path} 的 {role}）" for role, entries in source_roles.items() for value, path in entries)
        readings = "。".join(reading(output_label, role, source_roles) for role in source_roles)
        issues.append(f"「{output_label}」混放了來源中不同角色的值：{found}。{readings}")
    if not issues:
        return None
    return RunFailure(
        "mixed_source_roles",
        "同一欄位的值在來源中屬於不同角色。請依這個欄位的意義選擇下列其中一種改法，只修改指出的那一行，其他內容不變。"
        + " ".join(issues[:3]) + "。",
        correctable_fields=("content",),
    )


_MISSING = re.compile(
    r"未提供|未註明|未載明|無法計算|無法取得|缺少|缺失|不詳|"
    r"not provided|unavailable|missing|unknown|cannot be calculated", re.IGNORECASE)
_CLAUSE = re.compile(r"[，,。；;！!？?\n]")
_LABEL_DECORATION = re.compile(r"[*_`#>\s]+")


def _label(text: str) -> str:
    return _LABEL_DECORATION.sub("", text).strip(":：-")


def missing_item_labels(body: str) -> list[str]:
    """Labels the artifact marks as missing everywhere it uses them.

    Items are a table row's first cell or a 'label: value' line. A label that
    is missing for one entry but has a real value for another (for example a
    decision missing in one meeting and present in the other) is left out:
    a reply that names it may be describing the present value.
    """
    missing: list[str] = []
    present: set[str] = set()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 2 or all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            label, values = _label(cells[0]), cells[1:]
        else:
            match = re.match(r"^(?:[-*+]|\d+[.)])?\s*(.+?)\s*[:：]\s*(.+)$", stripped)
            if not match or _MISSING.search(match[1]):
                continue
            label, values = _label(match[1]), [match[2]]
        if any(_MISSING.search(value) for value in values):
            missing.append(label)
        else:
            present.add(label)
    return [label for label in dict.fromkeys(missing) if len(label) >= 2 and label not in present]


def completion_summary(summary: str, artifacts: list[str]) -> RunFailure | None:
    """The completion reply must agree with what was published.

    It sees only the reply and the published artifacts, never the task text.
    A clause that names an item the artifact marks as missing must also say it
    is missing, and numbers in the reply must appear in the artifact body.
    Single-digit integers are exempt: they describe the delivery itself
    ("2 份文件"), not a sourced value.
    """
    bodies = [artifact_body(content) for content in artifacts]
    issues: list[str] = []
    clauses = [clause for clause in _CLAUSE.split(summary) if clause.strip()]
    for label in (label for body in bodies for label in missing_item_labels(body)):
        claimed = [clause.strip() for clause in clauses if label in clause and not _MISSING.search(clause)]
        if claimed:
            issues.append(f"成果標示「{label}」缺項，但回覆「{claimed[0][:80]}」未說明它缺少")
    published = {value for body in bodies for match in _NUMBER.finditer(body)
                 if (value := _decimal(match.group())) is not None}
    for match in _NUMBER.finditer(summary):
        value = _decimal(match.group())
        if value is None or (value == value.to_integral_value() and abs(value) < 10 and "%" not in match.group()):
            continue
        if value not in published:
            issues.append(f"回覆的數值 {match.group()} 不在已發布成果中")
    if not issues:
        return None
    return RunFailure(
        "unsupported_completion_claim",
        "完成回覆必須與已發布成果一致；請只敘述成果實際包含的內容，並說明成果標示的缺項。" + "；".join(issues[:6]) + "。",
        correctable_fields=("summary",),
    )


_TABLE_SEPARATOR = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?")
_CITATION_MARK = re.compile(r"\[[^\[\]\n]*?\b(E[1-9][0-9]*)\]")


def _label_cells(body: str) -> list[tuple[list[str], str]]:
    """(candidate labels, cell) pairs: a table cell's column header and its row's first cell, or a line's label."""
    pairs: list[tuple[list[str], str]] = []
    lines = body.splitlines()
    headers: list[str] | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if headers is None and _TABLE_SEPARATOR.fullmatch(following):
                headers = [_label(cell) for cell in cells]
            elif headers is not None and not _TABLE_SEPARATOR.fullmatch(stripped):
                for position, cell in enumerate(cells):
                    labels = [headers[position]] if position < len(headers) else []
                    if position > 0:
                        labels.append(_label(cells[0]))
                    pairs.append((labels, cell))
            continue
        headers = None
        match = re.match(r"^(?:[-*+]|\d+[.)])?\s*(.+?)\s*[:：]\s*(.+)$", stripped)
        if match:
            pairs.append(([_label(match[1])], match[2]))
    return pairs


def table_columns(content: str, evidence: dict[str, Record]) -> RunFailure | None:
    """A value citing a table sample must not sit under another column's name.

    Applies only when one of the value's labels (its column header, its row's first cell, or
    its line label) is exactly a column of the cited table, and the value appears in that
    table's sample only under other columns. Only the artifact and tool evidence are read.
    """
    tables = {identity: record for identity, record in evidence.items()
              if isinstance(record.get("columns"), list) and isinstance(record.get("sample"), list)}
    if not tables:
        return None
    issues: list[str] = []
    for labels, cell in _label_cells(artifact_body(content)):
        cited = [identity for identity in _CITATION_MARK.findall(cell) if identity in tables]
        written = _CITATION_MARK.sub("", cell).strip()
        value = _plain(written)
        if not cited or not value:
            continue
        for identity in cited:
            table = tables[identity]
            by_name = {str(column).casefold(): str(column) for column in table["columns"]}
            named = [by_name[key] for key in ((_ANNOTATION.sub("", label).strip() or label).casefold() for label in labels)
                     if key in by_name]
            if not named:
                continue
            found = sorted({str(table["columns"][position]) for row in table["sample"] if isinstance(row, list)
                            for position, item in enumerate(row)
                            if position < len(table["columns"]) and _plain(str(item)) == value})
            if found and not set(named) & set(found):
                issue = (f"「{named[0]}」的 {_VALUE_EDGE.sub('', written)} 在 {table.get('path', identity)} 的"
                         f"「{'、'.join(found)}」欄，不在「{named[0]}」欄")
                if issue not in issues:
                    issues.append(issue)
    if not issues:
        return None
    return RunFailure(
        "column_mismatch",
        "表格數值放錯欄位：" + "；".join(issues[:3]) + "。請改為該欄位在來源中的值，或把值移到它所屬的欄位；"
        "只修改指出的儲存格，其他內容不變。",
        correctable_fields=("content",),
    )


DEFAULT_VERIFIERS: tuple[Verifier, ...] = (calculation_citations, consistent_source_roles, table_columns)


def verify_artifact(content: str, evidence: dict[str, Record],
                    verifiers: tuple[Verifier, ...] = DEFAULT_VERIFIERS) -> RunFailure | None:
    """Return the first verifier failure, so the model fixes one problem class at a time."""
    for verifier in verifiers:
        if (failure := verifier(content, evidence)) is not None:
            return failure
    return None
