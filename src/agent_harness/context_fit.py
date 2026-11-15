"""Fit one model request into the context window, in a fixed order of steps, or stop.

`RequestFitter.fit` measures the real request and, while it is over the bound, applies the next step,
re-measures and records a `context.pruned` event. The order is the policy:

 1. drop the oldest complete history turns of earlier Runs, then the optional source excerpts;
 2. drop narration next to successful tool groups, remove corrected or superseded tool errors, and move
    tool groups the model has already read into the saved executor state (every evidence ID and field kept);
 3. shorten the prompt-side tool catalogue (descriptions, then schema bounds), runtime guidance,
    inspection hints and artifact-name descriptions; the executor still enforces the full schemas;
 4. re-encode without loss: draft receipts, labelled-line views, JSON whitespace, a repeated plan;
 5. only for a Run that opted in: replace the largest evidence text with a labelled model summary
    (`compaction.py`); the evidence that verifiers read is not touched;
 6. still over: record `context.limited` and fail with `context_limit_exceeded`.

Nothing here decides what the task means; every step is about positions and sizes.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import compaction
from .context import (compact_tool_catalog, completed_tool_group, encode_draft_receipts, encode_duplicate_plan,
                      executor_state_message, omit_inspection_hints, omit_runtime_guidance, prune_resolved_tool_failures,
                      published_draft_group)
from .contracts import PendingCorrection, RunFailure, RunLimits

if TYPE_CHECKING:
    from .model import DecisionModel

Record = dict[str, Any]
SUMMARY_GUIDANCE = " 完成摘要只敘述已保存成果明確包含的內容；沒有標示缺項時，不宣稱已標示缺項。"


@dataclass
class Window:
    """The request being fitted: the conversation, the positions the loop tracks in it, and the offered tools."""

    messages: list[Record]
    tools: list[Record]
    current_start: int  # index of the first message of the current Run's own work
    acknowledged_until: int  # messages before this index were part of a request the model already answered
    canonical_index: int | None  # where the saved executor state message sits, once there is one
    history_turns: list[Record]
    excerpt_message: Record | None
    compactions: int = 0
    budget: Record = field(default_factory=dict)
    summary_guidance_applied: bool = False


def executor_context(navigation_state: Record, pending: dict[str, PendingCorrection], *,
                     include_inspection_hints: bool = True) -> tuple[Record, list[str]]:
    """The saved executor state message; while only artifact_write is being corrected, pinned artifact text is left out."""
    model_state = deepcopy(navigation_state)
    omitted_pinned_text: list[str] = []
    if bool(pending) and all(correction.call.name == "artifact_write" for correction in pending.values()):
        for evidence_id, evidence in model_state.get("verified_evidence", {}).items():
            if evidence.get("evidence_type") == "pinned_artifact" and isinstance(evidence.get("text"), str):
                del evidence["text"]
                omitted_pinned_text.append(evidence_id)
    return executor_state_message(model_state, include_inspection_hints=include_inspection_hints), omitted_pinned_text


class RequestFitter:
    """The context policy for one model decision of one Run."""

    def __init__(self, *, model: "DecisionModel", run: Record, limits: RunLimits, step: int,
                 perform: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
                 record: Callable[..., Awaitable[None]], read_tools: set[str], review_mode: bool, receipt_mode: bool,
                 published_this_run: bool, pending: dict[str, PendingCorrection], resolved_errors: set[str],
                 navigation_state: Record, compacting: bool, usage: dict[str, int]) -> None:
        self.model, self.run, self.limits, self.step = model, run, limits, step
        self.perform, self.record = perform, record
        self.read_tools, self.review_mode, self.receipt_mode = read_tools, review_mode, receipt_mode
        self.published_this_run, self.pending, self.resolved_errors = published_this_run, pending, resolved_errors
        self.navigation_state, self.compacting, self.usage = navigation_state, compacting, usage
        self.capacity = min(run["model"]["context_capacity_tokens"],
                            limits.context_window_tokens or run["model"]["context_capacity_tokens"])
        self.profile = run["model"].get("context_profile", "synthetic_fixture")

    def _over(self, window: Window) -> bool:
        return bool(window.budget["upper_bound_tokens"] > self.capacity)

    async def _budget(self, messages: list[Record], tools: list[Record]) -> Record:
        body = self.model.request_body(messages, tools, self.limits.max_output_tokens)
        budget: Record = await self.perform(lambda: self.model.context_budget(
            body, self.capacity, self.limits.max_output_tokens, self.profile))
        return budget

    async def _measure(self, window: Window) -> None:
        window.budget = await self._budget(window.messages, window.tools)

    async def _pruned(self, window: Window, data: Record) -> None:
        await self.record("context.pruned", {"step": self.step, **data, **window.budget})

    def _canonical(self, *, include_inspection_hints: bool = True) -> tuple[Record, list[str]]:
        return executor_context(self.navigation_state, self.pending, include_inspection_hints=include_inspection_hints)

    async def fit(self, window: Window) -> None:
        """Bring the request within the bound, or raise `context_limit_exceeded`; `window` is updated in place."""
        await self._measure(window)
        await self._drop_history(window)
        await self._drop_excerpts(window)
        await self._prune_groups(window)
        await self._shorten_catalogue(window)
        await self._omit_optional_views(window)
        await self._reencode(window)
        await self._compact_evidence(window)
        await self._add_summary_guidance(window)
        if self._over(window):
            await self.record("context.limited", {"step": self.step, **window.budget})
            raise RunFailure("context_limit_exceeded", "The complete request exceeds the configured conservative context envelope.")

    async def _drop_history(self, window: Window) -> None:
        while self._over(window) and window.history_turns:
            removed = window.history_turns.pop(0)
            count = len(removed["messages"])
            del window.messages[1:1 + count]
            window.current_start -= count
            window.acknowledged_until = max(0, window.acknowledged_until - count)
            if window.canonical_index is not None:
                window.canonical_index -= count
            await self._measure(window)
            await self._pruned(window, {"history_run_id": removed["run_id"], "reason": "request_budget"})

    async def _drop_excerpts(self, window: Window) -> None:
        index = next((index for index, message in enumerate(window.messages) if message is window.excerpt_message), None)
        if not self._over(window) or index is None:
            return
        del window.messages[index]
        window.current_start -= 1
        if window.acknowledged_until > index:
            window.acknowledged_until -= 1
        if window.canonical_index is not None and window.canonical_index > index:
            window.canonical_index -= 1
        await self._measure(window)
        await self._pruned(window, {"reason": "source_excerpt_budget"})

    async def _prune_groups(self, window: Window) -> None:
        """Narration first, then resolved errors, then groups the model has already read."""
        while self._over(window):
            offered = {tool["function"]["name"] for tool in window.tools}
            narration = completed_tool_group(window.messages, window.current_start, len(window.messages), offered,
                                             with_narration=True)
            if narration is not None:
                first, _, call_ids = narration
                text_bytes = len(window.messages[first]["content"].encode("utf-8"))
                window.messages[first]["content"] = None
                await self._measure(window)
                await self._pruned(window, {"call_ids": call_ids, "reason": "tool_narration", "text_utf8_bytes": text_bytes})
                continue
            navigation = completed_tool_group(window.messages, window.current_start, window.acknowledged_until,
                                              {"plan_update", "workspace_list"})
            reason = "completed_navigation"
            if navigation is None:
                # Keep the newest failed call in a correction chain, but discard older full arguments once a
                # newer retry has superseded them. Their IDs stay in `pending`, so correction auditing is complete.
                superseded_errors = {
                    call_id
                    for correction in self.pending.values()
                    for call_id, _, tool_name, error_code in correction.failure_records_with_codes[:-1]
                    if tool_name == "artifact_write"
                }
                compactable_errors = self.resolved_errors | superseded_errors
                window.messages, pruned_call_ids = prune_resolved_tool_failures(
                    window.messages, window.current_start, window.acknowledged_until, compactable_errors)
                if pruned_call_ids:
                    window.acknowledged_until = max(window.current_start, window.acknowledged_until - len(pruned_call_ids))
                    await self._measure(window)
                    reason = ("superseded_correction" if any(call_id in superseded_errors for call_id in pruned_call_ids)
                              else "resolved_correction")
                    await self._pruned(window, {"call_ids": pruned_call_ids, "reason": reason})
                    continue
                navigation = completed_tool_group(window.messages, window.current_start, window.acknowledged_until,
                                                  offered, resolved_error_ids=compactable_errors)
                if navigation is not None:
                    reason = ("superseded_correction" if any(call_id in superseded_errors for call_id in navigation[2])
                              else "resolved_correction")
            if navigation is None:
                navigation = completed_tool_group(window.messages, window.current_start, window.acknowledged_until,
                                                  self.read_tools)
                reason = "acknowledged_evidence"
            if navigation is None and self.review_mode:
                navigation = published_draft_group(window.messages, window.current_start, window.acknowledged_until,
                                                   self.read_tools)
                reason = "published_draft"
            if navigation is None:
                break
            first, after, call_ids = navigation
            if reason in {"acknowledged_evidence", "published_draft"}:
                ledger = self.navigation_state.setdefault("verified_evidence", {})
                for call, message in zip(window.messages[first]["tool_calls"], window.messages[first + 1:after], strict=True):
                    if call["function"]["name"] not in self.read_tools:
                        continue
                    result = json.loads(message["content"])
                    ledger[result["evidence_id"]] = result
            del window.messages[first:after]
            window.acknowledged_until -= after - first
            canonical, omitted_pinned_text = self._canonical()
            if window.canonical_index is None:
                window.canonical_index = window.current_start - 1
                window.messages.insert(window.canonical_index, canonical)
                window.current_start += 1
                window.acknowledged_until += 1
            else:
                window.messages[window.canonical_index] = canonical
            await self._measure(window)
            data: Record = {"call_ids": call_ids,
                            "reason": "artifact_correction_pinned_text" if omitted_pinned_text else reason,
                            "retained_executor_state": deepcopy(self.navigation_state)}
            extra: Record = ({"navigation_reason": reason, "omitted_pinned_artifact_text_ids": omitted_pinned_text}
                             if omitted_pinned_text else {})
            await self.record("context.pruned", {"step": self.step, **data, **window.budget, **extra})

    async def _shorten_catalogue(self, window: Window) -> None:
        """Tool descriptions, then draft receipts by reference, then schema bounds."""
        if self._over(window):
            window.tools = [{"type": tool["type"], "function": {
                key: value for key, value in tool["function"].items() if key != "description"
            }} for tool in window.tools]
            await self._measure(window)
            await self._pruned(window, {"reason": "tool_descriptions"})
        if self._over(window) and self.review_mode:
            encoded_calls = encode_draft_receipts(window.messages)
            if encoded_calls:
                await self._measure(window)
                await self.record("context.pruned", {"step": self.step, "reason": "draft_receipt_encoding",
                                                     "call_ids": encoded_calls, **window.budget})
        if self._over(window) and (self.review_mode or self.receipt_mode or self.published_this_run or self.pending):
            window.tools = compact_tool_catalog(window.tools)
            await self._measure(window)
            await self._pruned(window, {"reason": "tool_schema_bounds"})

    async def _omit_optional_views(self, window: Window) -> None:
        """Runtime guidance, inspection hints and artifact-name descriptions."""
        if self._over(window):
            omitted = omit_runtime_guidance(window.messages, self.navigation_state)
            if omitted:
                if window.canonical_index is not None:
                    window.messages[window.canonical_index] = self._canonical()[0]
                await self._measure(window)
                await self._pruned(window, {"reason": "runtime_guidance_budget", "omitted_result_views": omitted})
        if self._over(window):
            omitted = omit_inspection_hints(window.messages)
            if window.canonical_index is not None:
                omitted += sum("aggregatable_columns" in result
                               for result in self.navigation_state.get("verified_evidence", {}).values())
                if omitted:
                    window.messages[window.canonical_index] = self._canonical(include_inspection_hints=False)[0]
            if omitted:
                await self._measure(window)
                await self._pruned(window, {"reason": "inspection_capability_hints", "omitted_result_views": omitted})
        if self._over(window):
            compacted = deepcopy(window.tools)
            omitted_names = 0
            for tool in compacted:
                if tool["function"]["name"] in {"artifact_write", "artifact_draft"}:
                    if tool["function"]["parameters"]["properties"]["name"].pop("description", None) is not None:
                        omitted_names += 1
            if omitted_names:
                window.tools = compacted
                await self._measure(window)
                await self._pruned(window, {"reason": "artifact_name_descriptions", "omitted_descriptions": omitted_names})

    async def _reencode(self, window: Window) -> None:
        """Lossless re-encodings after publication, and a repeated plan in receipt mode."""
        if self._over(window) and self.published_this_run and window.canonical_index is not None:
            projected_state = deepcopy(self.navigation_state)
            evidence = projected_state.get("verified_evidence", {})
            labelled = [key for key, value in evidence.items() if "labelled_lines" in value and "text" in value]
            for key in labelled:
                del evidence[key]["labelled_lines"]
            if labelled:
                window.messages[window.canonical_index] = executor_state_message(projected_state)
                await self._measure(window)
                await self._pruned(window, {"reason": "labelled_lines_projection", "evidence_ids": labelled})
        if self._over(window) and self.published_this_run:
            compacted_results = 0
            for message in window.messages:
                if message.get("role") == "tool":
                    original = message["content"]
                    compact = json.dumps(json.loads(original), ensure_ascii=False, separators=(",", ":"))
                    if len(compact.encode("utf-8")) < len(original.encode("utf-8")):
                        message["content"] = compact
                        compacted_results += 1
            if compacted_results:
                await self._measure(window)
                await self._pruned(window, {"reason": "tool_result_json_whitespace", "compacted_results": compacted_results})
        if self._over(window) and self.receipt_mode and encode_duplicate_plan(window.messages):
            await self._measure(window)
            await self._pruned(window, {"reason": "duplicate_plan_encoding"})

    async def _compact_evidence(self, window: Window) -> None:
        """Opt-in and last: a labelled model summary replaces the largest evidence text in the model's view."""
        name = self.model.settings.name
        while self._over(window) and self.compacting and window.compactions < compaction.MAX_PER_RUN:
            target = compaction.largest_target(window.messages, window.current_start,
                                               self.navigation_state.get("verified_evidence", {}))
            if target is None:
                break
            target_kind, where, original = target

            async def summary_called(chunk: int, call_usage: Record | None,
                                     evidence_id: str = original["evidence_id"]) -> None:
                self.usage["model_calls"] += 1
                await self.record("compaction.model_responded", {
                    "step": self.step, "evidence_id": evidence_id, "chunk": chunk, "usage": call_usage}, usage=self.usage)

            try:
                summary, chunks = await self.perform(lambda: compaction.summarize(
                    self.model, self.run["task"], original, capacity=self.capacity, profile=self.profile,
                    on_call=summary_called))
            except RunFailure as error:
                await self.record("context.compaction_failed", {
                    "step": self.step, "evidence_id": original["evidence_id"], "reason": error.code, "message": error.message})
                break
            view = compaction.compacted_view(original, summary, name, chunks)
            if target_kind == "message":
                assert isinstance(where, int)
                window.messages[where]["content"] = json.dumps(view, ensure_ascii=False)
            else:
                self.navigation_state["verified_evidence"][where] = view
                if window.canonical_index is not None:
                    window.messages[window.canonical_index] = self._canonical()[0]
            window.compactions += 1
            await self._measure(window)
            await self.record("context.compacted", {
                "step": self.step, **compaction.summary_record(original, summary, name, chunks), **window.budget})

    async def _add_summary_guidance(self, window: Window) -> None:
        """After publication, add one line of completion guidance when it still fits."""
        window.summary_guidance_applied = False
        if self._over(window) or not self.published_this_run or self.receipt_mode:
            return
        candidate = deepcopy(window.messages)
        candidate[0]["content"] += SUMMARY_GUIDANCE
        budget = await self._budget(candidate, window.tools)
        if budget["upper_bound_tokens"] <= self.capacity:
            window.messages[0]["content"] = candidate[0]["content"]
            window.budget = budget
            window.summary_guidance_applied = True
