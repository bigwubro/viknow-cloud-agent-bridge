"""Eval run orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from hashlib import sha256
from time import perf_counter

from viknow.core.eval.candidate_evidence import EvidenceCollectionTask
from viknow.core.eval.recorder import (
    EvalModelCallTraceRecorder,
    activate_model_call_trace_recorder,
    reset_model_call_trace_recorder,
)
from viknow.core.eval.trace import EvalRunTrace, project_eval_trace
from viknow.core.observability.otel import (
    ATTR_PHASE,
    ATTR_RUN_ID,
    ERROR_EVAL_POLL_TIMEOUT,
    bind_run_id,
    record_error,
    reset_run_id,
    start_span,
)
from viknow.core.runtime.domain import (
    AgentOp,
    AgentOpType,
    KnowledgeAssetSelection,
    TraceCaptureMode,
)
from viknow.core.session.domain import RunStatus, UserNotFound
from viknow.core.session.manager import SessionManager
from viknow.core.session.store import SessionStore

_LANGFUSE_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class EvalRunTimeout(TimeoutError):
    """Raised when an eval run does not finish within the allotted time."""


class EvalService:
    """Runs synchronous eval requests against the shared agent runtime."""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        store: SessionStore,
        knowledge_adapter: object | None = None,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self.session_manager = session_manager
        self.store = store
        self.knowledge_adapter = knowledge_adapter
        self.poll_interval_seconds = poll_interval_seconds

    def resolve_eval_user_id(
        self,
        *,
        request_user_id: str | None = None,
        cached_user_id: str | None = None,
    ) -> str:
        if request_user_id:
            if self.store.get_user(request_user_id) is None:
                raise UserNotFound(request_user_id)
            return request_user_id
        configured = os.getenv("VIKNOW_EVAL_USER_ID", "").strip()
        if configured:
            if self.store.get_user(configured) is None:
                raise RuntimeError(f"configured eval user not found: {configured}")
            return configured
        if cached_user_id and self.store.get_user(cached_user_id) is not None:
            return cached_user_id
        return self.store.create_user(display_name="eval").id

    async def eval_run(
        self,
        *,
        query: str,
        profile_id: str | None = None,
        evidence_collection_task: EvidenceCollectionTask | None = None,
        timeout_seconds: float = 300.0,
        agent_model: str | None = None,
        requested_trace_id: str | None = None,
        trace_name: str | None = None,
        request_user_id: str | None = None,
        cached_eval_user_id: str | None = None,
        skill_flow_gold_trace: list[dict[str, object]] | None = None,
        selected_knowledge_assets: list[KnowledgeAssetSelection] | None = None,
        library_ids: list[str] | None = None,
        on_run_created: Callable[[str], None] | None = None,
    ) -> EvalRunTrace:
        deadline = perf_counter() + timeout_seconds
        user_id = self.resolve_eval_user_id(
            request_user_id=request_user_id,
            cached_user_id=cached_eval_user_id,
        )
        recorder = EvalModelCallTraceRecorder(
            run_started_at=perf_counter(),
        )
        langfuse_trace_id, langfuse_trace_source_id = _resolve_langfuse_trace_id(requested_trace_id)
        resolved_trace_name = _optional_non_empty(trace_name)
        token = activate_model_call_trace_recorder(recorder)
        try:
            created_run_id: str | None = None

            def remember_run_id(run_id: str) -> None:
                nonlocal created_run_id
                created_run_id = run_id
                if on_run_created is not None:
                    on_run_created(run_id)

            selected_assets = selected_knowledge_assets
            oracle_scope: dict[str, object] | None = None
            oracle_task: dict[str, object] | None = None
            message = query
            if evidence_collection_task is not None:
                selected_assets = [
                    KnowledgeAssetSelection(
                        library_id=evidence_collection_task.scope.library_id,
                        asset_id=asset_id,
                    )
                    for asset_id in evidence_collection_task.scope.asset_ids
                ] or None
                oracle_scope = evidence_collection_task.scope.model_dump(mode="json")
                oracle_task = evidence_collection_task.model_dump(mode="json")
                message = _evidence_oracle_message(query, evidence_collection_task)
            try:
                result = await asyncio.wait_for(
                    self.session_manager.create_run(
                        user_id=user_id,
                        session_id=None,
                        message=message,
                        profile_id=profile_id,
                        agent_model=agent_model,
                        trace_capture=TraceCaptureMode.FULL,
                        langfuse_trace_id=langfuse_trace_id,
                        langfuse_trace_source_id=langfuse_trace_source_id,
                        langfuse_trace_name=resolved_trace_name,
                        skill_flow_gold_trace=skill_flow_gold_trace,
                        selected_knowledge_assets=selected_assets,
                        library_ids=library_ids,
                        oracle_scope=oracle_scope,
                        oracle_task=oracle_task,
                        on_run_created=remember_run_id,
                    ),
                    timeout=_remaining_timeout(deadline),
                )
            except TimeoutError as exc:
                if created_run_id is not None:
                    await self._interrupt_timed_out_run(created_run_id)
                raise EvalRunTimeout(
                    f"eval run did not finish within {timeout_seconds:g} seconds"
                ) from exc
            run_token = bind_run_id(result.run_id)
            try:
                await self._wait_for_terminal(
                    result.run_id,
                    timeout_seconds=_remaining_timeout(deadline),
                )
            except EvalRunTimeout:
                record_error(ERROR_EVAL_POLL_TIMEOUT)
                await self._interrupt_timed_out_run(result.run_id)
                raise
            finally:
                reset_run_id(run_token)
            run = await asyncio.to_thread(self.session_manager.get_run, result.run_id)
            events = await asyncio.to_thread(self.store.events_after, result.run_id, 0)
            trace = await asyncio.to_thread(
                project_eval_trace,
                recorder,
                events,
                run,
                query,
                knowledge_adapter=self.knowledge_adapter,
            )
            return trace.model_copy(
                update={
                    "langfuse_trace_id": langfuse_trace_id,
                    "langfuse_trace_name": resolved_trace_name,
                }
            )
        finally:
            reset_model_call_trace_recorder(token)

    async def _wait_for_terminal(self, run_id: str, *, timeout_seconds: float) -> None:
        async with start_span(
            "eval.wait_terminal",
            **{ATTR_PHASE: "eval.wait_terminal", ATTR_RUN_ID: run_id},
        ):
            deadline = perf_counter() + timeout_seconds
            while perf_counter() < deadline:
                run = await asyncio.to_thread(self.store.get_run, run_id)
                if run is None:
                    raise RuntimeError(f"eval run not found: {run_id}")
                if run.status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.INTERRUPTED,
                }:
                    return
                await asyncio.sleep(self.poll_interval_seconds)
            record_error(ERROR_EVAL_POLL_TIMEOUT)
            raise EvalRunTimeout(
                f"eval run {run_id} did not finish within {timeout_seconds:g} seconds"
            )

    async def _interrupt_timed_out_run(self, run_id: str) -> None:
        try:
            runtime = await self.session_manager.runtime_for_run(run_id)
            await runtime.submit(AgentOp(type=AgentOpType.INTERRUPT, run_id=run_id))
        except Exception:
            # Preserve the timeout as the public failure even if best-effort cleanup fails.
            return


def _evidence_oracle_message(query: str, task: EvidenceCollectionTask) -> str:
    return "\n\n".join(
        (
            "EvidenceCollectionTask（Gate A 已批准，必须严格遵守）：",
            json.dumps(task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            "原始问题：",
            query,
        )
    )


def _resolve_langfuse_trace_id(requested_trace_id: str | None) -> tuple[str, str | None]:
    trace_id = (requested_trace_id or "").strip()
    if _LANGFUSE_TRACE_ID_PATTERN.fullmatch(trace_id):
        return trace_id, None
    if trace_id:
        return _deterministic_langfuse_trace_id(trace_id), trace_id
    return _deterministic_langfuse_trace_id(os.urandom(16).hex()), None


def _deterministic_langfuse_trace_id(seed: str) -> str:
    return sha256(seed.encode("utf-8")).digest()[:16].hex()


def _optional_non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _remaining_timeout(deadline: float) -> float:
    return max(0.0, deadline - perf_counter())
