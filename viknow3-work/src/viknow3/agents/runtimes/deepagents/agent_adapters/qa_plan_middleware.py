"""Checkpoint-owned QA planning, using the framework's native tools node."""

import json
from collections.abc import Callable
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import OmitFromInput
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from viknow3.agents.runtimes.deepagents.core.runtime.answer_plan import RunAnswerPlan
from viknow3.agents.runtimes.deepagents.core.runtime.answer_plan_tool import (
    ANSWER_PLAN_SCHEMA,
    AnswerPlanCommand,
    RecordedAnswerPlanReview,
    answer_plan_error_result,
)
from viknow3.agents.runtimes.deepagents.core.tools.base import ToolResult


class QaPlanSnapshot(BaseModel):
    """Task plan and current execution's assessment; evidence remains provenance-owned."""

    model_config = ConfigDict(extra="forbid")
    plan: RunAnswerPlan
    review: RecordedAnswerPlanReview | None = None
    run_id: str


class QaPlanObservation(BaseModel):
    """One completed framework tool call to fold into the plan after parallel join."""

    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    content: str | list


class QaPlanState(AgentState):
    qa_plan: Annotated[NotRequired[dict[str, Any]], OmitFromInput]
    qa_plan_observed_calls: Annotated[NotRequired[list[str]], OmitFromInput]


class QaPlanMiddleware(AgentMiddleware):
    """Bridge existing domain validation to graph state, never the resource scheduler.

    ``execute`` receives a detached snapshot and returns its validated successor and
    a ToolResult JSON string. ``project`` is strictly graph-to-run projection.
    ``observe`` folds a joined batch of newly completed evidence calls exactly once.
    No callback should write a second authoritative copy of the plan.
    """

    state_schema = QaPlanState
    propagate_to_subagents = False

    def __init__(
        self,
        *,
        run_id: str,
        original_question: str,
        execute: Callable[[QaPlanSnapshot, dict[str, Any]], tuple[QaPlanSnapshot, str]],
        project: Callable[[QaPlanSnapshot], None],
        view: Callable[[QaPlanSnapshot], dict[str, Any]],
        observe: Callable[[QaPlanSnapshot, list[QaPlanObservation]], QaPlanSnapshot] | None = None,
    ) -> None:
        super().__init__()
        self._run_id = run_id
        self._original_question = original_question
        self._execute = execute
        self._project = project
        self._view = view
        self._observe = observe

        def invoke(runtime: ToolRuntime, **args: Any) -> Command | ToolMessage:
            return self._invoke(runtime, args)

        async def ainvoke(runtime: ToolRuntime, **args: Any) -> Command | ToolMessage:
            return self._invoke(runtime, args)

        self.tools = [
            StructuredTool.from_function(
                name="answer_plan",
                description=ANSWER_PLAN_SCHEMA["description"],
                func=invoke,
                coroutine=ainvoke,
                args_schema=ANSWER_PLAN_SCHEMA["parameters"],
                infer_schema=False,
            )
        ]

    def before_agent(self, state: QaPlanState, runtime: Any) -> dict[str, Any]:
        stored = state.get("qa_plan")
        snapshot = (
            QaPlanSnapshot.model_validate(stored)
            if stored
            else QaPlanSnapshot(
                plan=RunAnswerPlan.initial(original_question=self._original_question),
                run_id=self._run_id,
            )
        )
        new_run = snapshot.run_id != self._run_id
        if new_run:
            snapshot = QaPlanSnapshot(
                plan=RunAnswerPlan.initial(original_question=self._original_question),
                run_id=self._run_id,
            )
        # A tools node can commit before its observations are folded. Retain the
        # checkpoint cursor on resume. A new run must not fold historical tool
        # results into its fresh plan, even if the old cursor was incomplete.
        observed = (
            state.get("qa_plan_observed_calls", [])
            if stored and not new_run
            else [m.tool_call_id for m in state["messages"] if isinstance(m, ToolMessage)]
        )
        self._project(snapshot.model_copy(deep=True))
        return {"qa_plan": snapshot.model_dump(mode="json"), "qa_plan_observed_calls": observed}

    async def abefore_agent(self, state: QaPlanState, runtime: Any) -> dict[str, Any]:
        return self.before_agent(state, runtime)

    def before_model(self, state: QaPlanState, runtime: Any) -> dict[str, Any] | None:
        snapshot = QaPlanSnapshot.model_validate(state["qa_plan"])
        seen = set(state.get("qa_plan_observed_calls", []))
        calls = {
            call["id"]: call
            for m in state["messages"]
            if isinstance(m, AIMessage)
            for call in m.tool_calls
        }
        fresh = [
            m
            for m in state["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id not in seen
        ]
        observations = [
            QaPlanObservation(
                tool_call_id=m.tool_call_id,
                tool_name=calls[m.tool_call_id]["name"],
                args=calls[m.tool_call_id]["args"],
                content=(
                    m.artifact["full_result"]
                    if isinstance(m.artifact, dict) and "full_result" in m.artifact
                    else m.content
                ),
            )
            for m in fresh
            if m.tool_call_id in calls and calls[m.tool_call_id]["name"] != "answer_plan"
        ]
        if observations and self._observe is not None:
            snapshot = self._observe(snapshot, observations)
        self._project(snapshot.model_copy(deep=True))
        if not fresh:
            return None
        return {
            "qa_plan": snapshot.model_dump(mode="json"),
            "qa_plan_observed_calls": [
                *state.get("qa_plan_observed_calls", []),
                *(m.tool_call_id for m in fresh),
            ],
        }

    async def abefore_model(self, state: QaPlanState, runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    def _model_request(self, request: Any) -> Any:
        snapshot = QaPlanSnapshot.model_validate(request.state["qa_plan"])
        summary = {
            "answer_plan": snapshot.plan.prompt_summary(),
            "review_is_current": self._view(snapshot)["review_is_current"],
        }
        # A current self-review is not publication authority. Keep its state
        # visible without repeatedly prompting an already performed action.
        guidance = ""
        if not summary["review_is_current"]:
            guidance = (
                "Record the knowledge-qa-v2 plan with answer_plan. "
                "Use action=start, update, or review with a content string and a findings "
                "array. Do not pass update objects, slot_updates, slot_definitions, or "
                "status enums. Keep citation aliases out of content and findings text. "
                "When the plan is ready, call final_check() with no arguments. "
                "Do not write the final answer as a separate message.\n"
            )
        return request.override(
            messages=[
                *request.messages,
                HumanMessage(
                    content=(
                        "<qa_answer_plan>\n"
                        + guidance
                        + json.dumps(summary, ensure_ascii=False)
                        + "\n</qa_answer_plan>"
                    )
                ),
            ]
        )

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        return handler(self._model_request(request))

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        return await handler(self._model_request(request))

    def _invoke(self, runtime: ToolRuntime, args: dict[str, Any]) -> Command | ToolMessage:
        snapshot = QaPlanSnapshot.model_validate(runtime.state["qa_plan"])
        try:
            command = AnswerPlanCommand.model_validate(args)
            # Tool calls run against the same superstep snapshot. Reject all
            # conflicting writes before returning any replacement state.
            latest = next(
                (m for m in reversed(runtime.state["messages"]) if isinstance(m, AIMessage)), None
            )
            writes = [
                call
                for call in (latest.tool_calls if latest else [])
                if call["name"] == "answer_plan" and call["args"].get("action") != "view"
            ]
            if command.action != "view" and len(writes) > 1:
                raise ValueError("Call answer_plan with at most one write per model turn.")
            if command.action == "view" and _has_unchanged_prior_view(
                runtime.state["messages"], self._view(snapshot)
            ):
                return ToolMessage(
                    ToolResult.fail(
                        "answer_plan",
                        "tool_gate_blocked",
                        "Answer Plan and evidence have not changed since the preceding view.",
                        detail="answer_plan_view_unchanged",
                        next_action={
                            "tool": "answer_plan",
                            "instruction": (
                                "Reuse the preceding view. Continue the investigation, update or "
                                "review the plan, or answer within the supported scope."
                            ),
                        },
                    ).to_json(),
                    tool_call_id=runtime.tool_call_id,
                    name="answer_plan",
                    status="error",
                )
            text_mode = (
                command.content is not None
                or command.findings is not None
                or command.template_id is not None
            )
            if command.action == "start" and not text_mode:
                snapshot.plan = RunAnswerPlan.initial(original_question=self._original_question)
                snapshot.review = None
                content = ToolResult.ok("answer_plan", self._view(snapshot)).to_json()
            else:
                snapshot, content = self._execute(snapshot, args)
        except (TypeError, ValueError) as exc:
            return ToolMessage(
                answer_plan_error_result(
                    exc,
                    current_plan={
                        "answer_plan": QaPlanSnapshot.model_validate(
                            runtime.state["qa_plan"]
                        ).plan.prompt_summary()
                    },
                ).to_json(),
                tool_call_id=runtime.tool_call_id,
                name="answer_plan",
                status="error",
            )
        failed = ToolResult.model_validate_json(content).error is not None
        message = ToolMessage(
            content,
            tool_call_id=runtime.tool_call_id,
            name="answer_plan",
            status="error" if failed else "success",
        )
        if failed:
            return message
        if command.action == "view":
            return message
        return Command(update={"qa_plan": snapshot.model_dump(mode="json"), "messages": [message]})


def _has_unchanged_prior_view(messages: list[Any], current_view: dict[str, Any]) -> bool:
    """Compare with successful views from earlier model turns in this checkpoint."""

    latest_ai_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], AIMessage)
        ),
        -1,
    )
    earlier_messages = messages[:latest_ai_index]
    actions = {
        call["id"]: call["args"].get("action")
        for message in earlier_messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "answer_plan"
    }
    for message in reversed(earlier_messages):
        if (
            not isinstance(message, ToolMessage)
            or message.name != "answer_plan"
            or message.status != "success"
            or actions.get(message.tool_call_id) != "view"
        ):
            continue
        try:
            result = ToolResult.model_validate_json(message.content)
        except (TypeError, ValueError):
            continue
        return result.error is None and result.data == current_view
    return False
