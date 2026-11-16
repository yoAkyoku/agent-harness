"""Exact retrieval inside one selected source: the best-matching lines, word for word, with line numbers.

`source_search` is for sources too large to read whole. It ranks a source's lines against the query
with BM25 over lowercase words and, for CJK text (which has no spaces), pairs of adjacent characters.
There is no embedding model and no index on disk: the source is read, checked against its admitted
hash and scored on each call, which is fast at this harness's source sizes.

What comes back is the source's own text, so it is evidence for the same file version as
`source_read` and nothing downstream changes. What retrieval cannot give is completeness: lines that
do not share words with the query are not returned. Because the search is deterministic, "no line of
this file version contains these words" is itself a checkable fact, so a search with no match is
evidence too, of exactly that and labelled as not being proof that the information is absent.
"""
from __future__ import annotations

import math
import re
from typing import Any

Record = dict[str, Any]
TOOL_NAME = "source_search"
MAX_RESULTS = 20
DEFAULT_RESULTS = 8
MAX_CONTEXT = 3
MAX_RESULT_BYTES = 8192
K1, B = 1.5, 0.75
PARAMETERS: Record = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 500},
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
        "context": {"type": "integer", "minimum": 0, "maximum": MAX_CONTEXT},
    },
    "required": ["path", "query"], "additionalProperties": False,
}
_CJK_RANGES = "\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
_WORD = re.compile(rf"[{_CJK_RANGES}]+|[^\W{_CJK_RANGES}]+")  # a run of CJK characters, or a word in any other script
_CJK = re.compile(rf"[{_CJK_RANGES}]")


def definition() -> Record:
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": ("Find the lines of one selected source that best match the query words, with line numbers and "
                        "neighbouring lines, word for word. Use it for a large source instead of reading it whole; cite the "
                        "result as [path EID]. No match does not prove the source lacks the information: try other words."),
        "parameters": PARAMETERS}}


def evidence_note(evidence: Record) -> str:
    """For an evidence appendix line: what a cited search covered, and that no match is not proof of absence."""
    if evidence.get("evidence_type") != "source_search":
        return ""
    if not evidence.get("matched_lines"):
        return f'; source_search "{evidence["query"]}": no line matched (a word search, not proof of absence)'
    return f'; source_search "{evidence["query"]}": {evidence["matched_lines"]} of {evidence["total_lines"]} lines matched'


def tokens(text: str) -> list[str]:
    """Lowercase words in any script; a run of CJK characters becomes every pair of adjacent characters."""
    result: list[str] = []
    for run in _WORD.findall(text.lower()):
        if _CJK.match(run) and len(run) > 1:
            result.extend(run[index:index + 2] for index in range(len(run) - 1))
        else:
            result.append(run)
    return result


def search_lines(text: str, query: str, *, max_results: int = DEFAULT_RESULTS, context: int = 1,
                 limit_bytes: int = MAX_RESULT_BYTES) -> Record:
    """The best lines for the query, in source order, within `limit_bytes`; a cut is reported.

    Matching lines are packed first, best first, so a tight limit drops the weakest matches; neighbouring
    lines are added only with the room that is left.
    """
    lines = text.splitlines()
    wanted = set(tokens(query))
    documents = [tokens(line) for line in lines]
    scored = [len(document) for document in documents if document]
    average = sum(scored) / len(scored) if scored else 1.0
    frequency = {term: sum(term in document for document in documents) for term in wanted}
    scores: list[tuple[float, int]] = []
    for number, document in enumerate(documents, 1):
        score = 0.0
        for term in wanted:
            count = document.count(term)
            if count:
                idf = math.log(1 + (len(scored) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * count * (K1 + 1) / (count + K1 * (1 - B + B * len(document) / average))
        if score > 0:
            scores.append((score, number))
    best = [number for _, number in sorted(scores, key=lambda item: (-item[0], item[1]))[:max_results]]
    chosen: set[int] = set()
    size = 0
    truncated = False

    def add(number: int) -> bool:
        nonlocal size
        extra = len(lines[number - 1].encode("utf-8")) + 1
        if size + extra > limit_bytes:
            return False
        chosen.add(number)
        size += extra
        return True

    for number in best:
        if not add(number):
            truncated = True
            break
    if not truncated:
        for number in best:
            for neighbour in range(max(1, number - context), min(len(lines), number + context) + 1):
                if neighbour not in chosen and not add(neighbour):
                    truncated = True
                    break
            if truncated:
                break
    ordered = sorted(chosen)
    return {"lines": ordered, "text": "\n".join(lines[number - 1] for number in ordered),
            "matched_lines": len(scores), "total_lines": len(lines), "truncated": truncated}
