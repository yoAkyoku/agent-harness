"""Require result provenance for explicit decimal expressions, not arbitrary prose."""

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
import re

from .contracts import RunFailure
from .storage import Record


_NUMBER = r"[+-]?(?:(?:\d{1,3}(?:,\d{3}){1,20}|\d{1,64})(?:\.\d{1,64})?|\.\d{1,64})(?:[eE][+-]?\d{1,6})?"
_EXPRESSION = re.compile(
    rf"(?<![A-Za-z0-9_.]){_NUMBER}(?:\s*[+*/×÷−-]\s*{_NUMBER}){{1,15}}\s*=\s*(?:\*\*)?(?P<value>{_NUMBER})(?![A-Za-z0-9_]|[.,]\d)"
)


def _equation_key(text: str) -> str:
    return re.sub(r"\s+|\*\*", "", text)


def require_csv_arithmetic_evidence(content: str, evidence: list[Record]) -> None:
    if not any(Path(record["path"]).suffix.lower() == ".csv" for record in evidence if "path" in record):
        return
    for path in sorted({record["path"] for record in evidence if "path" in record}, key=len, reverse=True):
        content = content.replace(path, "")
    values = {Decimal(record["value"]) for record in evidence if "value" in record}
    quoted: set[str] = set()
    for record in evidence:
        texts = [record.get("text", ""), *record.get("columns", [])]
        texts.extend(cell for row in record.get("sample", []) for cell in row)
        if Path(record.get("path", "")).suffix.lower() == ".csv" and "text" in record:
            try:
                texts.extend(cell for row in csv.reader(StringIO(record["text"]), strict=True) for cell in row)
            except csv.Error:
                pass
        for text in texts:
            quoted.update(_equation_key(expression.group()) for expression in _EXPRESSION.finditer(text))
    for expression in _EXPRESSION.finditer(content):
        if _equation_key(expression.group()) in quoted:
            continue
        try:
            value = Decimal(expression.group("value").replace(",", ""))
            if value.is_finite() and value in values:
                continue
        except InvalidOperation:
            pass
        raise RunFailure("invalid_evidence", "Call table_aggregate for totals or number_calculate for percentages first. Retry the rejected request with source and result IDs; leave missing inputs unknown.",
                         correctable_fields=("content", "citations"))
