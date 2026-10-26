"""Conservative run-local tool accounting through native middleware hooks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from pydantic import ValidationError

from agents.expert_buddy.core.runtime.domain import AgentEvent, AgentEventType, RuntimeBudgetConfig
from agents.expert_buddy.core.runtime.evidence_ledger import EvidenceLocatorFact, build_run_evidence_ledger
from agents.expert_buddy.core.runtime.tool_gate import ToolGateContext, evaluate_tool_gate
from agents.expert_buddy.core.tools.base import ToolRuntimeNotice, nested_tool_failure, unwrap_tool_data
from agents.expert_buddy.core.tools.context import get_tool_run_context

if TYPE_CHECKING:
    from agents.expert_buddy.agent_adapters.deepagents import _RunState

# Only read-only evidence tools qualify. Stateful plan/gate tools and arbitrary
# user tools are deliberately outside this policy.
_EVIDENCE_TOOLS = frozenset(
    {
        "knowledge_discover",
        "knowledge_search",
        "text_inspect",
        "image_inspect",
        "video_inspect",
        "video_inspect_2_0",
        "visual_ground",
        "list_video_segments",
        "list_doc_segments",
    }
)


@dataclass
class ToolEfficiencyState:
    attempts: dict[str, int] = field(default_factory=dict)
    in_flight: dict[str, str] = field(default_factory=dict)
    completed: dict[str, str] = field(default_factory=dict)
    terminal_failures: dict[str, str] = field(default_factory=dict)
    search_progress: dict[str, tuple[str, int]] = field(default_factory=dict)
    query_refine_calls: int = 0
    locating_calls: int = 0
    known_locators: set[str] = field(default_factory=set)
    repeated_candidates: list[EvidenceLocatorFact] = field(default_factory=list)
    repeated_scope: dict[str, Any] = field(default_factory=dict)
    available_candidates: list[EvidenceLocatorFact] = field(default_factory=list)
    inspected_sampling: dict[str, tuple[str, str]] = field(default_factory=dict)
    # V4: legal locators the runtime advised but that were never inspected.
    advised_locators: list[dict[str, Any]] = field(default_factory=list)
    # V4: distinct broad-search scopes already spent in this run.
    distinct_search_scopes: set[str] = field(default_factory=set)
    # V4: broad searches redirected into verifying an advised candidate.
    inspect_redirects: int = 0


def _key(name: str, args: dict[str, Any]) -> str:
    return name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload(result: Any, call_id: str) -> tuple[ToolMessage | None, dict[str, Any]]:
    if not isinstance(result, ToolMessage):
        update = getattr(result, "update", None)
        messages = update.get("messages", []) if isinstance(update, dict) else []
        result = next(
            (m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == call_id), None
        )
    if not isinstance(result, ToolMessage):
        return None, {}
    try:
        payload = json.loads(result.content)
    except (ValueError, TypeError):
        payload = {}
    return result, payload if isinstance(payload, dict) else {}


class ToolEfficiencyMiddleware(AgentMiddleware):
    propagate_to_subagents = False

    def __init__(self, state: _RunState, budget: RuntimeBudgetConfig):
        self.state, self.budget = state, budget

    def _blocked(self, request, detail: str, original_id: str = "", *, locators=None):
        call = request.tool_call
        message = (
            "Reuse the original tool result and its existing claim references; do not create "
            "new evidence from this notice. If the original call is still running, wait for it."
            if original_id
            else "The identical execution limit is reached. Use existing evidence or change a "
            "specific locator, modality, sampling parameter or unresolved question."
        )
        if detail == "inspect_stalled_candidate":
            message = (
                "Repeated locating added no new candidates. Inspect an available locator "
                "for the unresolved condition, or use a directed select query "
                "for adjacent/new evidence."
            )
        elif detail == "query_refine_execution_limit":
            message = (
                "The query_refine allowance is used. Reuse the original question and existing "
                "slots; inspect the missing evidence without another planning call."
            )
        elif detail == "broad_search_budget_exhausted":
            message = (
                "The broad-search allowance for this run is used. Do not open another search "
                "scope: inspect one of the already located candidates for the unresolved "
                "condition, or answer from the evidence you already hold."
            )
        self.state.repair_diagnostics.append(
            {
                "action": detail,
                "tool_call_id": call["id"],
                "tool": call["name"],
                "original_tool_call_id": original_id,
                "executed": False,
            }
        )
        return ToolMessage(
            name=call["name"],
            tool_call_id=call["id"],
            content=json.dumps(
                {
                    "tool": call["name"],
                    "error": {
                        "code": "tool_gate_blocked",
                        "detail": detail,
                        "message": message,
                        "original_tool_call_id": original_id,
                        "retryable": False,
                        "available_locators": locators or [],
                    },
                }
            ),
        )

    def _result_locators(self, name, arguments, payload):
        event = AgentEvent(
            type=AgentEventType.TOOL_CALL_COMPLETED,
            run_id=self.state.run_id,
            sequence=0,
            payload={
                "id": "candidate_projection",
                "name": name,
                "arguments": json.dumps(arguments),
                "result": json.dumps(payload),
            },
        )
        ledger = build_run_evidence_ledger([event])
        return [locator for manifest in ledger.candidate_manifests for locator in manifest.locators]

    def _inspect_targets(self, arguments=None):
        state = self.state.tool_efficiency
        if self.budget.gap_driven_tools:
            # An unbound candidate is not proof of relevance to the pending slot.
            # Offer it immediately below, without forcing an arbitrary inspection.
            return []
        if (
            not self.budget.candidate_inspect_after
            or state.locating_calls < self.budget.candidate_inspect_after
        ):
            return []
        if arguments and any(
            arguments.get(k)
            and state.repeated_scope.get(k)
            and arguments[k] != state.repeated_scope[k]
            for k in ("library_id", "asset_id")
        ):
            return []
        return self._legal_targets(state.repeated_candidates)

    def _legal_targets(self, candidates):
        run_context = get_tool_run_context()
        context = (
            run_context.tool_gate_context_factory()
            if run_context and run_context.tool_gate_context_factory
            else ToolGateContext(
                ledger=self.state.evidence_ledger,
                completed_tool_calls=list(self.state.completed_tool_calls.values()),
            )
        )
        targets = []
        for locator in candidates:
            if not locator.asset_id or not locator.time_range:
                continue
            target = {
                "library_id": locator.library_id,
                "asset_id": locator.asset_id,
                "evidence_id": locator.segment_id or locator.evidence_id,
                "time_range": locator.time_range,
            }
            if (
                evaluate_tool_gate(context, tool_name="video_inspect_2_0", arguments=target).allowed
                and target not in targets
            ):
                targets.append(target)
        return targets

    def _pending_advised_targets(self) -> list[dict[str, Any]]:
        """Advice already given that no inspection has consumed yet."""
        return [dict(locator) for locator in self.state.tool_efficiency.advised_locators][:3]

    def _execution_key(self, name, arguments):
        meaningful = {k: v for k, v in arguments.items() if k != "title"}
        if self.budget.gap_driven_tools and name in {"list_video_segments", "list_doc_segments"}:
            mode = arguments.get("mode")
            # Keep malformed values and unknown fields for native schema validation.
            if (
                isinstance(mode, str)
                and mode in {"map", "page"}
                and isinstance(meaningful.get("query"), str)
            ):
                meaningful.pop("query")
        return _key(name, meaningful)

    def _gap_advice(self, request):
        names = {
            item.get("function", item).get("name")
            if isinstance(item, dict)
            else getattr(item, "name", "")
            for item in request.tools
        }
        notice = (
            "Do not call query_refine by default. Reuse the immutable original question and "
            "its answer slots; refine once only when decomposition resolves a real ambiguity. "
            "Before another broad search or catalog map, identify the missing condition that "
            "can change the original option choice. Reuse completed catalog pages and claims. "
            "A different question over the same sampling is not independent evidence; a new "
            "attribute, modality, endpoint, crop or fps may still need verification. "
            "Do not use visual symbols as proof of words spoken. "
        )
        targets = (
            self._legal_targets(self.state.tool_efficiency.available_candidates)[:3]
            if "video_inspect_2_0" in names
            else []
        )
        if targets:
            context = get_tool_run_context()
            slots = (
                context.answer_slots_factory() if context and context.answer_slots_factory else []
            )
            # V4: remember what was advised so a later pass can verify that at
            # least one advised candidate was actually inspected.
            self.state.tool_efficiency.advised_locators = [dict(target) for target in targets]
            notice += (
                "Legal candidates are available now. Prefer inspecting one only if it decides "
                "a pending original-question condition. Copy its identity and absolute bounds; "
                "use directed adjacent selection for missing windows. Legality alone does not "
                "prove relevance. Candidates: " + json.dumps(targets, ensure_ascii=False)
            )
            self.state.repair_diagnostics.append(
                {
                    "action": "candidate_inspect_advice",
                    "available_locators": targets,
                    "slot_ids": [slot["id"] for slot in slots if slot.get("id")],
                    "enforced": False,
                }
            )
        blocks = request.system_message.content_blocks if request.system_message else []
        return request.override(
            system_message=SystemMessage(content_blocks=[*blocks, {"type": "text", "text": notice}])
        )

    def prepare_model(self, request):
        if not self.budget.enabled or not request.tools:
            return request
        if self.budget.gap_driven_tools:
            request = self._gap_advice(request)
        limit = self.budget.max_query_refine_calls
        refine_closed = limit is not None and self.state.tool_efficiency.query_refine_calls >= limit
        targets = self._inspect_targets()
        hidden = ({"query_refine"} if refine_closed else set()) | (
            {"knowledge_search", "query_refine"} if targets else set()
        )
        if not hidden:
            return request

        def name(item):
            return (
                item.get("function", item).get("name")
                if isinstance(item, dict)
                else getattr(item, "name", "")
            )

        tools = [item for item in request.tools if name(item) not in hidden]
        notice = "Reuse the existing original-question plan; do not repeat query_refine."
        if targets:
            notice += (
                " Repeated locating is stalled. Inspect the decisive unresolved condition "
                "at one of these legal locators. list_video_segments remains available only for "
                "directed select with a query; do not map/page/coverage again. Locators: "
                + json.dumps(targets, ensure_ascii=False)
            )
        blocks = request.system_message.content_blocks if request.system_message else []
        return request.override(
            tools=tools,
            tool_choice=None,
            system_message=SystemMessage(
                content_blocks=[*blocks, {"type": "text", "text": notice}]
            ),
        )

    def wrap_model_call(self, request, handler):
        return handler(self.prepare_model(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self.prepare_model(request))

    def _record_progress(self, name, arguments, payload):
        state = self.state.tool_efficiency
        if name in {
            "text_inspect",
            "image_inspect",
            "video_inspect",
            "video_inspect_2_0",
            "visual_ground",
        }:
            state.locating_calls = 0
            state.repeated_candidates = []
            # V4: an advised candidate that is now inspected is no longer pending.
            state.advised_locators = [
                locator
                for locator in state.advised_locators
                if not (
                    locator.get("asset_id") == arguments.get("asset_id")
                    and locator.get("time_range") == arguments.get("time_range")
                )
            ]
            if self.budget.gap_driven_tools:
                state.available_candidates = [
                    locator
                    for locator in state.available_candidates
                    if not (
                        locator.asset_id == arguments.get("asset_id")
                        and locator.time_range == arguments.get("time_range")
                    )
                ]
        elif name in {
            "knowledge_search",
            "list_video_segments",
            "list_doc_segments",
            "query_refine",
        }:
            state.locating_calls += 1
            locators = self._result_locators(name, arguments, payload)
            if self.budget.gap_driven_tools and locators:
                state.available_candidates = locators
            identities = {_key("locator", locator.model_dump()) for locator in locators}
            if identities - state.known_locators:
                state.locating_calls = 1
                state.repeated_candidates = []
            state.known_locators.update(identities)

    def _search_notice(self, arguments: dict[str, Any], payload: dict[str, Any]):
        threshold = self.budget.no_progress_warning_after
        data = unwrap_tool_data(payload)
        candidates = data.get("evidence")
        if (not threshold and not self.budget.candidate_inspect_after) or not isinstance(
            candidates, list
        ):
            return None
        # Keep content in the signature: the same locator with genuinely new
        # text is progress. Ranking scores alone are not new evidence.
        signatures = sorted(
            _key(
                "candidate",
                {k: v for k, v in item.items() if k not in {"score", "rank", "relevance_score"}},
            )
            for item in candidates
            if isinstance(item, dict)
        )
        digest = hashlib.sha256(json.dumps(signatures).encode()).hexdigest()
        scope_arguments = {k: v for k, v in arguments.items() if k not in {"query", "title"}}
        scope = _key("search", scope_arguments)
        previous, repeats = self.state.tool_efficiency.search_progress.get(scope, ("", 0))
        repeats = repeats + 1 if previous == digest else 1
        self.state.tool_efficiency.search_progress[scope] = (digest, repeats)
        if self.budget.candidate_inspect_after:
            state = self.state.tool_efficiency
            state.repeated_candidates = (
                self._result_locators("knowledge_search", arguments, payload)
                if repeats >= 2
                else []
            )
            state.repeated_scope = scope_arguments
            if previous != digest:
                state.locating_calls = 1
        if not threshold or repeats < threshold:
            return None
        notice = {
            "code": "search_no_progress",
            "consecutive_same_results": repeats,
            "message": "These searches added no new candidate or content. Inspect an existing "
            "candidate that decides an unresolved condition. If none is usable, change an "
            "explicit locator condition or modality; do not merely paraphrase broad searches.",
        }
        self.state.repair_diagnostics.append({"action": "search_no_progress", **notice})
        return notice

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        limited_refine = (
            call["name"] == "query_refine" and self.budget.max_query_refine_calls is not None
        )
        if not self.budget.enabled or (call["name"] not in _EVIDENCE_TOOLS and not limited_refine):
            return await handler(request)
        arguments = {k: v for k, v in call["args"].items() if k != "run_progress"}
        if (
            self.budget.gap_driven_tools
            and call["name"] in {"list_video_segments", "list_doc_segments"}
            and arguments.get("mode") == "coverage"
        ):
            return await handler(request.override(tool_call={**call, "args": arguments}))
        request = request.override(tool_call={**call, "args": arguments})
        if limited_refine:
            schema = getattr(request.tool, "tool_call_schema", None)
            if callable(getattr(schema, "model_validate", None)):
                try:
                    schema.model_validate(arguments)
                except ValidationError:
                    # Let the native tool node render its schema error, without
                    # spending the allowance for an actual refinement execution.
                    return await handler(request)
        key = self._execution_key(call["name"], arguments)
        state = self.state.tool_efficiency
        if limited_refine and state.query_refine_calls >= self.budget.max_query_refine_calls:
            return self._blocked(request, "query_refine_execution_limit")
        targets = self._inspect_targets(arguments)
        broad = call["name"] in {"knowledge_search", "query_refine"} or (
            call["name"] == "list_video_segments"
            and not (
                arguments.get("mode") == "select" and str(arguments.get("query") or "").strip()
            )
        )
        if targets and broad:
            return self._blocked(request, "inspect_stalled_candidate", locators=targets)
        if broad:
            # V4: when the runtime already advised a legal locator that was never
            # inspected, spend this turn on that verification instead of another
            # search. This is a substitution, not an extra call: the model
            # redirects the tool call it was going to make anyway. Bounded.
            pending = self._pending_advised_targets()
            limit = self.budget.max_inspect_redirects
            if pending and limit is not None and state.inspect_redirects < limit:
                state.inspect_redirects += 1
                self.state.repair_diagnostics.append(
                    {
                        "action": "inspect_advice_redirect",
                        "tool": call["name"],
                        "redirect_index": state.inspect_redirects,
                        "locators": pending,
                        "enforced": True,
                    }
                )
                return self._blocked(request, "inspect_stalled_candidate", locators=pending)
        if broad and self.budget.max_broad_scopes is not None:
            # V4: distinct broad-search scopes are capped per run. Repeating the
            # same scope stays free; a new scope past the cap must become a
            # verification of an existing candidate instead.
            scope = _key(
                "search", {k: v for k, v in arguments.items() if k not in {"query", "title"}}
            )
            if scope not in state.distinct_search_scopes:
                if len(state.distinct_search_scopes) >= self.budget.max_broad_scopes:
                    return self._blocked(
                        request,
                        "broad_search_budget_exhausted",
                        locators=self._legal_targets(state.available_candidates)[:1],
                    )
                state.distinct_search_scopes.add(scope)
        if key in state.completed:
            return self._blocked(request, "reuse_completed_result", state.completed[key])
        if key in state.in_flight:
            return self._blocked(request, "identical_call_in_flight", state.in_flight[key])
        if key in state.terminal_failures:
            return self._blocked(
                request, "repeat_nonretryable_execution", state.terminal_failures[key]
            )
        limit = self.budget.max_identical_attempts
        if limit is not None and state.attempts.get(key, 0) >= limit:
            return self._blocked(request, "identical_execution_limit")
        # Reserve synchronously before awaiting so one model turn cannot start
        # parallel copies. State lives on the run, not this graph instance.
        state.in_flight[key] = call["id"]
        state.attempts[key] = state.attempts.get(key, 0) + 1
        if limited_refine:
            state.query_refine_calls += 1
        sampling_key = ""
        sampling_notice = None
        if (
            self.budget.gap_driven_tools
            and call["name"]
            in {
                "video_inspect",
                "video_inspect_2_0",
                "image_inspect",
                "text_inspect",
                "visual_ground",
            }
            and arguments.get("asset_id")
        ):
            sampling_key = _key(
                call["name"],
                {
                    k: v
                    for k, v in arguments.items()
                    if k not in {"title", "question", "instruction"}
                },
            )
            previous = state.inspected_sampling.get(sampling_key)
            focus = _key("focus", {k: arguments.get(k) for k in ("question", "instruction")})
            if previous and previous[1] != focus:
                sampling_notice = (
                    "The same source and sampling were inspected before. The rewording alone "
                    "does not establish independent evidence; resolve the remaining attribute "
                    "or change crop, sampling or endpoint when needed."
                )
                self.state.repair_diagnostics.append(
                    {
                        "action": "same_sampling_new_question",
                        "tool_call_id": call["id"],
                        "original_tool_call_id": previous[0],
                        "executed": True,
                        "sampling_key": sampling_key,
                    }
                )
        try:
            result = await handler(request)
            message, payload = _payload(result, call["id"])
            error = unwrap_tool_data(payload).get("error")
            if isinstance(error, dict) and (
                error.get("code") == "tool_gate_blocked"
                or (limited_refine and error.get("code") == "invalid_arguments")
            ):
                state.attempts[key] -= 1
                if limited_refine:
                    state.query_refine_calls -= 1
                return result
            nested = nested_tool_failure(payload)
            if not error and nested is not None:
                if nested["retryable"] is False:
                    state.terminal_failures[key] = call["id"]
                # Keep the actual attempt charged, but neither cache success nor
                # clear locating stagnation. Unknown retries stay within the cap.
                return result
            if error or (message and message.status == "error"):
                if not isinstance(error, dict) or error.get("retryable") is not True:
                    state.terminal_failures[key] = call["id"]
                return result
            if message is not None:
                state.completed[key] = call["id"]
                self._record_progress(call["name"], arguments, payload)
                if sampling_key:
                    state.inspected_sampling[sampling_key] = (call["id"], focus)
                    self.state.repair_diagnostics.append(
                        {
                            "action": "inspect_result",
                            "tool_call_id": call["id"],
                            "sampling_key": sampling_key,
                            "result_sha256": hashlib.sha256(message.content.encode()).hexdigest(),
                        }
                    )
                if sampling_notice and isinstance(result, ToolMessage):
                    result = result.model_copy(
                        update={
                            "content": json.dumps(
                                {
                                    **payload,
                                    "runtime_notices": [
                                        *payload.get("runtime_notices", []),
                                        ToolRuntimeNotice(
                                            kind="same_sampling_new_question",
                                            message=sampling_notice,
                                        ).model_dump(mode="json"),
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    )
            if call["name"] == "knowledge_search":
                notice = self._search_notice(arguments, payload)
                if notice and isinstance(result, ToolMessage):
                    return result.model_copy(
                        update={
                            "content": json.dumps(
                                {
                                    **payload,
                                    "runtime_notices": [
                                        *payload.get("runtime_notices", []),
                                        ToolRuntimeNotice(
                                            kind="search_no_progress", message=notice["message"]
                                        ).model_dump(mode="json"),
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    )
            return result
        finally:
            state.in_flight.pop(key, None)
