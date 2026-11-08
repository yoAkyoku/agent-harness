from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    context_window_tokens: int | None = Field(default=None, ge=1024, le=16777216)
    # Thinking models (for example Claude) spend output tokens on reasoning first,
    # so remote runs may need more than local 8K-context models.
    max_output_tokens: int = Field(default=1024, ge=128, le=32768)
    max_excerpt_bytes: int = Field(default=512, ge=0, le=8192)
    max_memory_bytes: int = Field(default=1024, ge=0, le=8192)
    max_history_bytes: int = Field(default=16384, ge=0, le=65536)
    max_steps: int = Field(default=12, ge=1, le=30)
    max_tool_calls: int = Field(default=20, ge=1, le=50)
    max_corrections: int = Field(default=2, ge=0, le=5)
    timeout_seconds: float = Field(default=180, gt=0, le=600)
    approval_timeout_seconds: float = Field(default=120, gt=0, le=600)


@dataclass(frozen=True)
class ToolCall:
    identity: str
    name: str
    arguments: dict[str, Any]


def canonical_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(arguments)
    if name == "source_read" and result.get("labels") == []:
        del result["labels"]
    return result


@dataclass(frozen=True)
class PreparedCall:
    call: ToolCall
    arguments: dict[str, Any]
    scope: dict[str, Any]

    def digest(self, run_id: str) -> str:
        value = {"run_id": run_id, "call_id": self.call.identity, "tool": self.call.name,
                 "arguments": self.arguments, "scope": self.scope}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PendingCorrection:
    call: ToolCall
    decision_step: int
    correctable_fields: tuple[str, ...] = ()
    prior: tuple["PendingCorrection", ...] = ()
    error_code: str | None = None

    @property
    def failure_records(self) -> tuple[tuple[str, int], ...]:
        earlier = tuple(record for correction in self.prior for record in correction.failure_records)
        return (*earlier, (self.call.identity, self.decision_step))

    @property
    def failure_records_with_codes(self) -> tuple[tuple[str, int, str, str | None], ...]:
        earlier = tuple(record for correction in self.prior for record in correction.failure_records_with_codes)
        return (*earlier, (self.call.identity, self.decision_step, self.call.name, self.error_code))

    def matches(self, candidate: ToolCall, decision_step: int) -> bool:
        if decision_step <= self.decision_step:
            return False
        if self.call.name == candidate.name:
            original = canonical_tool_arguments(self.call.name, self.call.arguments)
            proposed = canonical_tool_arguments(candidate.name, candidate.arguments)
            return all(field in self.correctable_fields or proposed.get(field) == value
                       for field, value in original.items())
        if ((self.call.name == "source_read" and self.call.arguments.get("labels"))
                or (candidate.name == "source_read" and candidate.arguments.get("labels"))):
            return False
        # Raw views retrieve the same admitted source; they do not replace an explicit query or calculation.
        source = self.call.arguments.get("path")
        source_views = {"source_read", "table_inspect"}
        return (source is not None and source == candidate.arguments.get("path")
                and self.call.name in source_views and candidate.name in source_views)


@dataclass(frozen=True)
class Decision:
    text: str
    calls: tuple[ToolCall, ...]
    message: dict[str, Any]
    usage: dict[str, int] | None = None


class RunFailure(Exception):
    def __init__(self, code: str, message: str, *, correctable_fields: tuple[str, ...] = (),
                 diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.correctable_fields = correctable_fields
        self.diagnostics = diagnostics


class RunCancelled(Exception):
    """The Run's user cancellation token was observed by its executor."""
