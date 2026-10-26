"""Run-scoped progress and bounded exploration using native middleware hooks."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from agents.expert_buddy.core.answer_review.choice import (
    ProvisionalChoice,
    RunProgress,
    explicit_choice_checkpoint,
    parse_choice_options,
)
from agents.expert_buddy.core.runtime.answer_plan_tool import evidence_digest
from agents.expert_buddy.core.runtime.domain import RuntimeBudgetConfig
from agents.expert_buddy.core.tools.context import (
    get_tool_run_context,
    reset_tool_run_context,
    set_tool_run_context,
)

if TYPE_CHECKING:
    from agents.expert_buddy.agent_adapters.deepagents import _RunState

_PROGRESS_TOOLS = frozenset(
    {
        "knowledge_search",
        "text_inspect",
        "image_inspect",
        "video_inspect",
        "video_inspect_2_0",
        "list_video_segments",
    }
)
# Pure final review is still allowed; it grants no new evidence or budget.
_LOCAL_REVIEW_TOOLS = frozenset({"stop_gate_check", "answer_plan"})


def _claim_snapshot(state) -> dict[str, str]:
    return {
        claim.claim_ref: hashlib.sha256(
            claim.model_dump_json(exclude={"source_event_sequence"}).encode()
        ).hexdigest()
        for claim in state.evidence_ledger.claims
    }


def choice_evidence_status(state, question: str) -> dict[str, Any]:
    """Report evidence awaiting a decision without inventing an option or assessment."""
    choice = state.provisional_choice
    if choice is not None and choice.original_question != question:
        choice = None
    current = _claim_snapshot(state)
    previous = choice.evidence_snapshot if choice else {}
    changed = sorted(ref for ref, digest in current.items() if previous.get(ref) != digest)
    removed = sorted(set(previous) - set(current))
    return {
        "option_id": choice.option_id if choice else None,
        "decision_id": choice.tool_call_id if choice else None,
        "supporting_claim_refs": choice.supporting_claim_refs if choice else [],
        "conflicting_claim_refs": choice.conflicting_claim_refs if choice else [],
        "pending_claim_refs": changed,
        "removed_claim_refs": removed,
        "decision_pending": bool(changed or removed),
        "verified": False,
    }


def observe_choice_stream(state, event: dict, *, text: str = "") -> None:
    """Join stream messages to model runs explicitly; producer and consumer may race."""
    kind = event.get("event")
    run_id = str(event.get("run_id") or "")
    if not run_id or kind not in {
        "on_chat_model_start",
        "on_chat_model_stream",
        "on_chat_model_end",
    }:
        return
    if kind == "on_chat_model_start":
        state.choice_model_order.setdefault(run_id, len(state.choice_model_order) + 1)
    data = event.get("data") or {}
    message = data.get("chunk" if kind == "on_chat_model_stream" else "output")
    message_id = getattr(message, "id", None)
    if message_id:
        state.choice_message_runs[str(message_id)] = run_id
    if text:
        state.choice_stream_text[run_id] = state.choice_stream_text.get(run_id, "") + text


def newer_stream_choice(state, question: str) -> tuple[bool, str | None, dict]:
    """A newer incomplete model may supersede an older completed choice or withdrawal."""
    source_run = state.choice_message_runs.get(state.choice_source_message_id, "")
    source_order = state.choice_model_order.get(source_run)
    diagnostic = {
        "source": "completed_model_decision",
        "message_id": state.choice_source_message_id,
        "model_run_id": source_run,
        "model_order": source_order,
    }
    # A completed producer response can be ahead of the consumer queue.
    if state.choice_decision_seen and source_order is None:
        diagnostic["ordering_unavailable"] = True
        return False, None, diagnostic
    options = {item.option_id for item in parse_choice_options(question)}
    for run_id in reversed(state.choice_model_order):
        order = state.choice_model_order[run_id]
        if order <= (source_order or 0):
            break
        found, option_id = explicit_choice_checkpoint(state.choice_stream_text.get(run_id, ""))
        if found and (option_id is None or option_id in options):
            return (
                True,
                option_id,
                {
                    "source": "newer_model_stream",
                    "model_run_id": run_id,
                    "model_order": order,
                    "option_id": option_id,
                },
            )
    return False, None, diagnostic


class BudgetEfficiencyMiddleware(AgentMiddleware):
    propagate_to_subagents = False

    def __init__(
        self,
        state: _RunState,
        budget: RuntimeBudgetConfig,
        *,
        question: Callable[[], str],
        step_limit: Callable[[], int],
    ):
        self.state, self.budget = state, budget
        self.question, self.step_limit = question, step_limit

    def exploration_seconds(self) -> float:
        return (
            self.budget.time_limit_seconds
            - self.budget.finalization_reserve_seconds
            - (time.monotonic() - self.state.budget_started_at)
        )

    def finalizing(self) -> bool:
        if not self.budget.enabled:
            return False
        return (
            self.state.budget_finalizing
            or (self.budget.finalization_reserve_seconds > 0 and self.exploration_seconds() <= 0)
            or (
                self.budget.finalization_reserve_steps > 0
                and self.state.recursion_step
                >= self.step_limit() - self.budget.finalization_reserve_steps
            )
        )

    def current_choice(self) -> ProvisionalChoice | None:
        choice = self.state.provisional_choice
        return choice if choice and choice.original_question == self.question() else None

    def choice_evidence_status(self) -> dict[str, Any]:
        return choice_evidence_status(self.state, self.question())

    def capture_progress(
        self,
        payload: Any,
        call_id: str,
        *,
        source: str = "tool_metadata",
        evidence_snapshot: dict[str, str] | None = None,
        source_message_id: str = "",
    ) -> None:
        if not self.budget.capture_provisional_choice:
            return
        try:
            progress = RunProgress.model_validate(payload)
            if progress.option_id is None:
                self.state.choice_decision_seen = True
                self.state.choice_source_message_id = source_message_id
                self.state.provisional_choice = None
                self.state.best_effort_answer = ""
                self.state.pending_final_candidate_text = ""
                self.state.choice_draft_start = len(self.state.streamed_text)
                self.state.repair_diagnostics.append(
                    {
                        "action": "provisional_choice_withdrawn",
                        "tool_call_id": call_id,
                        "source": source,
                    }
                )
                return
            option = next(
                (
                    o
                    for o in parse_choice_options(self.question())
                    if o.option_id == progress.option_id
                ),
                None,
            )
            if option is None:
                raise ValueError("option_id is not in the original question")
            facts = {c.claim_ref: c for c in self.state.evidence_ledger.claims}
            history = {c.claim_ref for c in self.state.historical_claims}
            if (
                not set(progress.supporting_claim_refs)
                <= {ref for ref, fact in facts.items() if fact.status == "verified"} | history
            ):
                raise ValueError("support contains an unavailable or unverified claim")
            if not set(progress.conflicting_claim_refs) <= set(facts) | history:
                raise ValueError("conflict contains an unavailable claim")
            self.state.choice_decision_seen = True
            self.state.choice_source_message_id = source_message_id
            self.state.provisional_choice = ProvisionalChoice(
                **option.model_dump(),
                original_question=self.question(),
                evidence_digest=evidence_digest(self.state.evidence_ledger),
                evidence_snapshot=(
                    _claim_snapshot(self.state) if evidence_snapshot is None else evidence_snapshot
                )
                if self.budget.choice_evidence_handoff
                else {},
                tool_call_id=call_id,
                **progress.model_dump(exclude={"option_id"}),
            )
            self.state.repair_diagnostics.append(
                {
                    "action": "provisional_choice_recorded",
                    "source": source,
                    "tool_call_id": call_id,
                    "choice": self.state.provisional_choice.model_dump(mode="json"),
                    "verified": False,
                }
            )
        except (ValueError, TypeError):
            # Metadata must never invalidate otherwise valid tool arguments.
            self.state.repair_diagnostics.append(
                {
                    "action": "provisional_choice_rejected",
                    "tool_call_id": call_id,
                    "reason": "invalid_option_or_claim_metadata",
                }
            )

    def _capture_response(self, response: Any, *, evidence_snapshot=None) -> Any:
        # Capture in model output order, before parallel tools start; completion
        # order can never overwrite a more recent decision.
        for message in getattr(response, "result", []):
            if isinstance(message, AIMessage):
                found, option_id = explicit_choice_checkpoint(message.text)
                previous = self.current_choice()
                if found and (previous is None or previous.option_id != option_id):
                    # A bare repeated label cannot erase recorded gaps/support
                    # or mark old evidence freshly assessed. Rich metadata can.
                    self.capture_progress(
                        {"option_id": option_id},
                        str(
                            message.id
                            or (
                                message.tool_calls[0]["id"]
                                if message.tool_calls
                                else "model_decision"
                            )
                        ),
                        source="explicit_model_decision",
                        evidence_snapshot=evidence_snapshot,
                        source_message_id=str(message.id or ""),
                    )
                elif found and previous is not None and previous.option_id == option_id:
                    # Reaffirmation has a newer position, but does not assess new claims.
                    self.state.choice_source_message_id = str(message.id or "")
                for call in message.tool_calls:
                    if call["name"] in _PROGRESS_TOOLS and "run_progress" in call["args"]:
                        self.capture_progress(
                            call["args"]["run_progress"],
                            call["id"],
                            evidence_snapshot=evidence_snapshot,
                            source_message_id=str(message.id or ""),
                        )
        return response

    def prepare_model(self, request: Any) -> Any:
        if not self.budget.enabled:
            return request
        notice = ""
        if self.finalizing():
            if not self.state.budget_finalizing:
                self.state.repair_diagnostics.append(
                    {
                        "action": "finalization_reserve",
                        "verified": False,
                        "remaining_seconds": max(
                            0, self.exploration_seconds() + self.budget.finalization_reserve_seconds
                        ),
                    }
                )
            self.state.budget_finalizing = True
            # V5 fix 1: the reserved finalization window must yield one answer, not
            # another blocked tool turn. The step is text-only and the notice says so
            # explicitly, then asks for the single terminating line, so the reserved
            # steps are spent emitting `Final answer: <letter>`.
            request = request.override(tools=[], tool_choice=None)
            notice = (
                "Exploration is closed inside the ORIGINAL run budget. Tools are disabled for "
                "this step: do not request a tool and do not plan further investigation. Use "
                "the evidence already gathered and answer now. For a single-choice question "
                "reply with exactly one line that ends the response: `Final answer: <letter>`, "
                "using the original option ID and choosing the best-supported option. Missing "
                "evidence stays unverified; do not invent citations."
            )
        elif self.budget.capture_provisional_choice and parse_choice_options(self.question()):
            tools = []
            for item in request.tools:
                name = getattr(item, "name", None)
                if isinstance(item, dict):
                    name = item.get("function", item).get("name")
                if name not in _PROGRESS_TOOLS:
                    tools.append(item)
                    continue
                schema = copy.deepcopy(convert_to_openai_tool(item)["function"])
                schema.setdefault("parameters", {}).setdefault("properties", {})["run_progress"] = (
                    RunProgress.model_json_schema()
                )
                tools.append(schema)
            request = request.override(tools=tools)
            notice = (
                "After evidence, include a standalone Current choice: <letter> line in each "
                "normal decision, or Current choice: unknown if no choice is supported. Use the "
                "original option ID; update or withdraw it when evidence changes. This is a "
                "provisional checkpoint, not verified evidence. Do not add a model or tool turn "
                "just to record it. "
                "When deciding a normal evidence tool call, optionally record run_progress: "
                "current option_id, supporting/conflicting claim_refs and missing_conditions. "
                "Use null option_id to withdraw a candidate. This is provisional, not evidence; "
                "do not add a separate call to record it."
            )
        choice = self.current_choice()
        if choice:
            notice += "\nProvisional choice (not verified): " + json.dumps(
                {
                    **choice.model_dump(
                        exclude={"original_question", "evidence_digest", "evidence_snapshot"}
                    ),
                    "stale": (
                        self.choice_evidence_status()["decision_pending"]
                        if self.budget.choice_evidence_handoff
                        else choice.evidence_digest != evidence_digest(self.state.evidence_ledger)
                    ),
                },
                ensure_ascii=False,
            )
        if self.budget.choice_evidence_handoff and self.budget.capture_provisional_choice:
            status = self.choice_evidence_status()
            if status["decision_pending"]:
                notice += (
                    "\nEvidence changed since the last recorded choice. On this normal decision, "
                    "assess it against the original options; update or withdraw your choice. "
                    "A repeated letter alone does not record an evidence assessment. "
                    "Record supporting/conflicting claim_refs in run_progress when applicable. "
                    + json.dumps(status, ensure_ascii=False)
                )
        if notice:
            blocks = request.system_message.content_blocks if request.system_message else []
            request = request.override(
                system_message=SystemMessage(
                    content_blocks=[*blocks, {"type": "text", "text": notice}]
                )
            )
        return request

    def wrap_model_call(self, request, handler):
        snapshot = _claim_snapshot(self.state) if self.budget.choice_evidence_handoff else None
        return self._capture_response(
            handler(self.prepare_model(request)), evidence_snapshot=snapshot
        )

    async def awrap_model_call(self, request, handler):
        snapshot = _claim_snapshot(self.state) if self.budget.choice_evidence_handoff else None
        return self._capture_response(
            await handler(self.prepare_model(request)), evidence_snapshot=snapshot
        )

    @staticmethod
    def _strip_progress(request):
        call = request.tool_call
        if call["name"] not in _PROGRESS_TOOLS or "run_progress" not in call["args"]:
            return request
        return request.override(
            tool_call={
                **call,
                "args": {k: v for k, v in call["args"].items() if k != "run_progress"},
            }
        )

    def _reserved_result(self, request, *, cancellation_requested=False):
        self.state.budget_finalizing = True
        self.state.repair_diagnostics.append(
            {
                "action": "tool_wait_stopped_for_finalization",
                "tool_call_id": request.tool_call["id"],
                "tool": request.tool_call["name"],
                "verified": False,
                "cancellation_requested": cancellation_requested,
                "upstream_completion_unknown": cancellation_requested,
            }
        )
        return ToolMessage(
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            content=json.dumps(
                {
                    "tool": request.tool_call["name"],
                    "error": {
                        "code": "tool_gate_blocked",
                        "detail": "finalization_reserve",
                        "message": "Exploration deadline reached. Finalize from existing evidence.",
                        "retryable": False,
                    },
                }
            ),
        )

    async def awrap_tool_call(self, request, handler):
        request = self._strip_progress(request)
        if not self.budget.enabled or request.tool_call["name"] in _LOCAL_REVIEW_TOOLS:
            return await handler(request)
        if self.finalizing():
            return self._reserved_result(request)
        if self.budget.finalization_reserve_seconds <= 0:
            return await handler(request)
        context = get_tool_run_context()
        token = None
        if context:
            original = context.remaining_seconds_factory

            def remaining():
                total = original() if original else None
                available = self.exploration_seconds()
                return min(total, available) if total is not None else available

            token = set_tool_run_context(replace(context, remaining_seconds_factory=remaining))
        task = asyncio.create_task(handler(request))
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, self.exploration_seconds()))
            if done:
                return task.result()
            task.cancel()
            task.add_done_callback(_consume_task)
            return self._reserved_result(request, cancellation_requested=True)
        except BaseException:
            task.cancel()
            task.add_done_callback(_consume_task)
            raise
        finally:
            if token is not None:
                reset_tool_run_context(token)


def _consume_task(task):
    if not task.cancelled():
        task.exception()
