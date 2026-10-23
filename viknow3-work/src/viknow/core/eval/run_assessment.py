"""Build the deterministic input package for one self-learning run analysis."""

from __future__ import annotations

from time import perf_counter

from viknow.core.eval.analysis import RunAssessmentFact, RunAssessmentPackage, TraceReference
from viknow.core.eval.recorder import EvalModelCallTraceRecorder
from viknow.core.eval.skill_flow import SkillEvalCase, score_case
from viknow.core.eval.trace import project_eval_trace
from viknow.core.runtime.domain import AgentEvent, AgentEventType
from viknow.core.session.domain import RunRecord


def build_run_assessment_package(
    run: RunRecord,
    events: list[AgentEvent],
    *,
    reference_answer: str = "",
    knowledge_adapter: object | None = None,
) -> RunAssessmentPackage:
    """Project persisted run events once, then apply the deterministic Skill Flow scorer."""

    trace = project_eval_trace(
        EvalModelCallTraceRecorder(run_started_at=perf_counter()),
        events,
        run,
        run.message,
        knowledge_adapter=knowledge_adapter,
    )
    scored = score_case(
        SkillEvalCase(
            case_id=run.id,
            question=run.message,
            reference_answer=reference_answer,
        ),
        trace,
    ).to_dict()
    answer_event = _latest(events, AgentEventType.MESSAGE_COMPLETED)
    facts = [
        RunAssessmentFact(
            fact_id=f"{run.id}:status",
            statement=f"运行状态为 {scored['status']}。",
            event_sequences=_terminal_sequences(events),
        ),
        RunAssessmentFact(
            fact_id=f"{run.id}:verdict",
            statement=(
                "确定性流程评分结论为 "
                f"{scored['verdict_result'].get('verdict', 'unknown')}；"
                "该结论不证明最终答案的事实正确性。"
            ),
            event_sequences=_terminal_sequences(events),
        ),
        RunAssessmentFact(
            fact_id=f"{run.id}:evaluation_stage",
            statement=(
                "当前评估阶段为 accepted，已有参考答案。"
                if reference_answer
                else "当前评估阶段为 provisional，没有参考答案，不能判定答案事实正确。"
            ),
        ),
    ]
    if answer_event is not None:
        facts.append(
            RunAssessmentFact(
                fact_id=f"{run.id}:final_answer",
                statement="已记录最终回答及其引用元数据。",
                event_sequences=[answer_event.sequence],
            )
        )
    for bucket in scored["failure_buckets"]:
        facts.append(
            RunAssessmentFact(
                fact_id=f"{run.id}:failure_bucket:{bucket}",
                statement=f"确定性评分命中失败桶 {bucket}。",
                event_sequences=_related_sequences(events, bucket),
            )
        )
    references = _trace_references(events)
    missing_inputs: list[str] = []
    if trace.answer is None:
        missing_inputs.append("final_answer")
    if not references:
        missing_inputs.append("langfuse_trace")
    if not reference_answer:
        missing_inputs.append("reference_answer")
    return RunAssessmentPackage(
        run_id=run.id,
        question=run.message,
        answer=trace.answer,
        evaluation_stage="accepted" if reference_answer else "provisional",
        deterministic_result=scored,
        facts=facts,
        trace_references=references,
        missing_inputs=missing_inputs,
    )


def _latest(events: list[AgentEvent], event_type: AgentEventType) -> AgentEvent | None:
    return next((event for event in reversed(events) if event.type == event_type), None)


def _terminal_sequences(events: list[AgentEvent]) -> list[int]:
    terminal = {
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_FAILED,
        AgentEventType.RUN_INTERRUPTED,
    }
    return [event.sequence for event in events if event.type in terminal]


def _related_sequences(events: list[AgentEvent], bucket: str) -> list[int]:
    if "citation" in bucket or "evidence" in bucket:
        relevant = {
            AgentEventType.MESSAGE_COMPLETED,
            AgentEventType.ANSWER_REVIEW_COMPLETED,
            AgentEventType.STOP_GATE_EVALUATED,
        }
        return [event.sequence for event in events if event.type in relevant]
    return _terminal_sequences(events)


def _trace_references(events: list[AgentEvent]) -> list[TraceReference]:
    references: list[TraceReference] = []
    seen: set[tuple[str, str, str]] = set()
    for event in reversed(events):
        if event.type not in {
            AgentEventType.RUN_COMPLETED,
            AgentEventType.RUN_FAILED,
            AgentEventType.RUN_INTERRUPTED,
        }:
            continue
        links = event.payload.get("trace_links")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict) or link.get("state") != "resolved":
                continue
            if link.get("provider") != "langfuse":
                continue
            values = tuple(
                str(link.get(key) or "") for key in ("source_id", "project_id", "trace_id")
            )
            if not all(values) or values in seen:
                continue
            seen.add(values)
            references.append(
                TraceReference(source_id=values[0], project_id=values[1], trace_id=values[2])
            )
    return references
