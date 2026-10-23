"""DeepAgents-backed semantic analysis for a bound run assessment package."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from time import monotonic
from uuid import uuid4

from viknow.core.eval.analysis import RunAssessmentPackage
from viknow.core.eval.trace_analysis_tools import TraceAnalysisToolSession
from viknow.core.learning.case_review import OptimizationProposalAnalysis
from viknow.core.observability.clickhouse_trace_reader import ClickHouseTraceReader
from viknow.core.runtime.domain import AgentEventType, AgentOp, AgentOpType, AgentProfile
from viknow.core.runtime.events import AgentRuntime

RuntimeFactory = Callable[[AgentProfile, tuple], AgentRuntime]

SELF_LEARNING_ANALYSIS_TRACE_NAME = "viknow2-self-learning-analysis"


class RunTraceProposalAnalyzer:
    """Run the internal analysis profile and return the existing proposal contract."""

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory,
        reader: ClickHouseTraceReader | None,
        model: str,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._reader = reader
        self.model = model

    def analyze_run(
        self,
        package: RunAssessmentPackage,
        *,
        on_activity: Callable[[str, str], None] | None = None,
    ) -> OptimizationProposalAnalysis:
        return asyncio.run(self._analyze_run(package, on_activity=on_activity))

    async def _analyze_run(
        self,
        package: RunAssessmentPackage,
        *,
        on_activity: Callable[[str, str], None] | None = None,
    ) -> OptimizationProposalAnalysis:
        if package.trace_references and self._reader is None:
            raise RuntimeError("ClickHouse trace reader is not configured for bound traces")
        readers = (
            {reference.source_id: self._reader for reference in package.trace_references}
            if self._reader is not None
            else {}
        )
        session = TraceAnalysisToolSession(package=package, readers=readers)
        profile = AgentProfile(
            id="self-learning-analyst",
            label="运行分析器",
            description="内部使用的 ViKnow 运行语义归因器",
            enabled_tools=(
                "trace_materialize",
                "read_file",
                "grep",
                "execute",
                "submit_run_analysis",
            ),
            enabled_skills=("self-learning-run-analysis",),
            answer_review={"enabled": False, "gate_enabled": False},
            runtime_budget={
                "enabled": True,
                "max_recursion_steps": 180,
                "time_limit_seconds": 900,
                "warn_ratio": 0.8,
            },
            system_prompt=(
                "You are an internal ViKnow trace analyst. Read the active skill, inspect only "
                "the bound sources, and finish by calling submit_run_analysis."
            ),
        )
        runtime = self._runtime_factory(profile, session.tool_specs())
        analysis_run_id = f"analysis-{uuid4()}"
        thinking = ""
        last_activity_at = 0.0
        try:
            await runtime.submit(
                AgentOp(
                    type=AgentOpType.USER_INPUT,
                    run_id=analysis_run_id,
                    langfuse_trace_name=SELF_LEARNING_ANALYSIS_TRACE_NAME,
                    input=json.dumps(package.model_dump(mode="json"), ensure_ascii=False),
                    memory_enabled=False,
                    use_precompiled_knowledge=False,
                )
            )
            while True:
                event = await runtime.next_event(analysis_run_id)
                if event.type == AgentEventType.REASONING_DELTA:
                    thinking = (thinking + str(event.payload.get("text") or ""))[-4000:]
                    now = monotonic()
                    if on_activity is not None and thinking and now - last_activity_at >= 0.25:
                        on_activity("think", thinking)
                        last_activity_at = now
                elif event.type == AgentEventType.TOOL_CALL_STARTED:
                    if on_activity is not None:
                        if thinking:
                            on_activity("think", thinking)
                        on_activity("tool", str(event.payload.get("name") or "工具调用"))
                    thinking = ""
                    last_activity_at = 0.0
                elif event.type == AgentEventType.REASONING_COMPLETED:
                    if on_activity is not None and thinking:
                        on_activity("think", thinking)
                if event.type in {
                    AgentEventType.RUN_COMPLETED,
                    AgentEventType.RUN_FAILED,
                    AgentEventType.RUN_INTERRUPTED,
                }:
                    if event.type != AgentEventType.RUN_COMPLETED and session.result is None:
                        error = event.payload.get("error") or event.payload.get("message")
                        error_code = (
                            error.get("code")
                            if isinstance(error, dict)
                            else str(error or "unknown")[:500]
                        )
                        raise RuntimeError(
                            f"run analysis agent ended with {event.type.value}: {error_code}"
                        )
                    break
        finally:
            await runtime.aclose()
        if session.result is None:
            raise ValueError("run analysis agent did not submit a structured result")
        result = session.result
        direction = {
            "skill": "skill_instruction",
            "harness": "harness_policy",
            "tool_or_index": "tool_or_index",
            "capability_gap": "knowledge_source",
            "evidence_insufficient": "knowledge_source",
        }[result.primary_category]
        return OptimizationProposalAnalysis(
            issue_summary=_legacy_projection(result.summary, limit=2_000),
            handling_direction=direction,
            target_skill_id="knowledge-qa-v2" if direction == "skill_instruction" else None,
            optimization_suggestion=_legacy_projection(
                result.optimization_suggestion,
                limit=4_000,
            ),
            confidence=result.confidence,
            requires_oracle=result.requires_oracle,
            oracle_reason=(
                "分析建议补充参考答案或证据后再确定优化输入。" if result.requires_oracle else None
            ),
            trace_analysis=result,
        )


def _legacy_projection(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n\n[完整内容请查看 trace_analysis]"
    return value[: limit - len(marker)].rstrip() + marker
