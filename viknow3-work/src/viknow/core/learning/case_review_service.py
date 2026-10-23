"""Application service for the first human learning gate."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from viknow.core.answer_review.domain import AnswerReviewReplayInput
from viknow.core.eval.candidate_evidence import (
    CandidateEvidencePackage,
    GateBDecision,
    build_accepted_oracle_package,
)
from viknow.core.eval.run_assessment import build_run_assessment_package
from viknow.core.learning.candidates import learning_candidate_risk, select_learning_candidates
from viknow.core.learning.case_review import (
    ACTIVE_SKILL_OPTIMIZATION_STATUSES,
    AnswerReviewReplayOutcome,
    ApprovedCaseSnapshot,
    CandidateCase,
    CaseReviewDecision,
    OptimizationAnalysisTask,
    OptimizationProposal,
    OptimizationProposalAnalysis,
    OptimizationProposalAnalysisInput,
    OracleGateBReview,
    OracleRequest,
    ReviewStatus,
    SkillOptimizationExperiment,
    SkillOptimizationExperimentConfig,
    SkillOptimizationTask,
)
from viknow.core.learning.case_review_store import CaseReviewStore
from viknow.core.learning.projection import project_learning_record
from viknow.core.learning.skill_optimization_view import (
    SkillOptimizationExperimentView,
    SkillOptimizationView,
    load_skill_optimization_experiment_view,
    load_skill_optimization_view,
)
from viknow.core.runtime.domain import AgentEventType
from viknow.core.session.domain import RunStatus
from viknow.core.session.store import SessionStore


class OptimizationProposalAnalyzer(Protocol):
    model: str

    def analyze(
        self, analysis_input: OptimizationProposalAnalysisInput
    ) -> OptimizationProposalAnalysis: ...


class AnswerReviewReplayer(Protocol):
    def replay(self, replay_input: AnswerReviewReplayInput) -> dict: ...


class CaseReviewService:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        review_store: CaseReviewStore,
        proposal_analyzer: OptimizationProposalAnalyzer | None = None,
        answer_review_replayer: AnswerReviewReplayer | None = None,
        target_skill_id: str = "knowledge-qa-v2",
        skillopt_experiment_root: Path | None = None,
    ) -> None:
        self._session_store = session_store
        self._review_store = review_store
        self._proposal_analyzer = proposal_analyzer
        self._answer_review_replayer = answer_review_replayer
        self._target_skill_id = target_skill_id
        self._skillopt_experiment_root = skillopt_experiment_root

    def add_manual_candidate(self, run_id: str, *, actor: str) -> CandidateCase:
        run = self._require_terminal_run(run_id)
        candidate = self._review_store.add_candidate(run_id=run_id, source="manual", added_by=actor)
        return candidate.model_copy(update={"question": run.message[:20_000]})

    def proposal(self, run_id: str) -> OptimizationProposal:
        proposal = self._review_store.get_proposal(run_id)
        if proposal is None:
            raise KeyError(run_id)
        return proposal

    def analyze_proposal(
        self, run_id: str, *, task_id: str | None = None, task_attempt: int | None = None
    ) -> OptimizationProposal:
        try:
            proposal = self.proposal(run_id)
        except KeyError:
            proposal = self.start_proposal_analysis(run_id)
        analyzer = self._proposal_analyzer
        if analyzer is None:
            return self._review_store.fail_proposal(run_id, error_code="AnalyzerNotConfigured")
        events = self._session_store.events_after(run_id, after_sequence=0)
        run = self._require_terminal_run(run_id)
        completed = next(
            (event for event in reversed(events) if event.type == AgentEventType.MESSAGE_COMPLETED),
            None,
        )
        review = next(
            (
                event
                for event in reversed(events)
                if event.type == AgentEventType.ANSWER_REVIEW_COMPLETED
            ),
            None,
        )
        payload = completed.payload if completed is not None else {}
        citations = payload.get("citations") or {}
        if task_id is None:
            self._review_store.mark_proposal_running(run_id)
        try:
            review_payload = review.payload if review is not None else {}
            replay_outcome = self._replay_timed_out_answer_review(review_payload)
            if replay_outcome is not None:
                review_payload = replay_outcome.result
            analysis_input = OptimizationProposalAnalysisInput(
                run_id=run_id,
                question=run.message,
                answer=payload.get("text"),
                bucket=proposal.bucket,
                risk=proposal.risk,
                reason_codes=proposal.reason_codes,
                answer_review=review_payload,
                citation_count=len(citations),
            )
            analyze_run = getattr(analyzer, "analyze_run", None)
            if analyze_run is not None:

                def activity_callback(kind: str, text: str) -> None:
                    if task_id is None or task_attempt is None:
                        return
                    self._review_store.update_analysis_activity(
                        task_id, attempt=task_attempt, kind=kind, text=text
                    )

                analysis = analyze_run(
                    build_run_assessment_package(
                        run,
                        events,
                        knowledge_adapter=None,
                    ),
                    **({"on_activity": activity_callback} if task_id is not None else {}),
                )
            else:
                analysis = analyzer.analyze(analysis_input)
            if analysis.handling_direction == "skill_instruction" and (
                analysis.target_skill_id is None
            ):
                analysis = analysis.model_copy(update={"target_skill_id": self._target_skill_id})
        except Exception as exc:
            logger.bind(run_id=run_id, task_id=task_id).exception(
                "[learning] optimization analysis execution failed"
            )
            if task_id is not None:
                if task_attempt is not None:
                    self._review_store.update_analysis_activity(
                        task_id,
                        attempt=task_attempt,
                        kind="error",
                        text=_public_analysis_error(exc),
                    )
                return proposal.model_copy(
                    update={"status": "failed", "error_code": type(exc).__name__}
                )
            return self._review_store.fail_proposal(run_id, error_code=type(exc).__name__)
        if task_id is not None:
            if task_attempt is None:
                raise ValueError("analysis task attempt is required")
            self._review_store.commit_analysis_result(
                task_id,
                attempt=task_attempt,
                analysis=analysis,
                analysis_model=analyzer.model,
                answer_review_replay=replay_outcome,
            )
            return self.proposal(run_id)
        return self._review_store.complete_proposal(
            run_id,
            analysis=analysis,
            analysis_model=analyzer.model,
            answer_review_replay=replay_outcome,
        )

    def _replay_timed_out_answer_review(
        self, review_payload: dict
    ) -> AnswerReviewReplayOutcome | None:
        error = review_payload.get("error")
        if not isinstance(error, dict) or error.get("code") != "answer_review_timeout":
            return None
        if review_payload.get("retryable") is not True or error.get("retryable") is not True:
            return None
        replayer = self._answer_review_replayer
        if replayer is None:
            raise RuntimeError("answer review replayer is not configured")
        try:
            replay_input = AnswerReviewReplayInput.model_validate(
                review_payload.get("review_input")
            )
        except ValidationError as exc:
            raise ValueError("invalid answer review replay input") from exc
        result = replayer.replay(replay_input)
        return AnswerReviewReplayOutcome(
            reviewer_model=replay_input.reviewer_model,
            result=result,
        )

    def start_proposal_analysis(self, run_id: str) -> OptimizationProposal:
        self._require_terminal_run(run_id)
        events = self._session_store.events_after(run_id, after_sequence=0)
        selected = select_learning_candidates([project_learning_record(events)])[0]
        risk = learning_candidate_risk(selected)
        candidate = self._review_store.get_candidate(run_id)
        if candidate is not None and candidate.active:
            self._review_store.update_candidate_classification(
                run_id=run_id,
                bucket=selected.bucket,
                reason_codes=selected.reason_codes,
            )
        return self._review_store.create_proposal(
            run_id=run_id,
            bucket=selected.bucket,
            risk=risk,
            reason_codes=selected.reason_codes,
        )

    def enqueue_proposal_analysis(self, run_id: str) -> OptimizationAnalysisTask:
        self.start_proposal_analysis(run_id)
        return self._review_store.enqueue_analysis_task(run_id=run_id)

    def latest_analysis_task(self, run_id: str) -> OptimizationAnalysisTask | None:
        return self._review_store.latest_analysis_task(run_id)

    def cancel_proposal_analysis(self, run_id: str, *, task_id: str) -> OptimizationAnalysisTask:
        task = self._review_store.get_analysis_task(task_id)
        if task is None or task.run_id != run_id:
            raise KeyError(task_id)
        return self._review_store.cancel_analysis_task(task_id)

    def process_pending_analysis_once(
        self, *, limit: int, lease_seconds: float, max_attempts: int = 3
    ) -> int:
        processed = 0
        for task in self._review_store.claim_analysis_tasks(
            limit=limit, lease_seconds=lease_seconds
        ):
            logger.bind(task_id=task.id, run_id=task.run_id, attempt=task.attempts).info(
                "[learning] optimization analysis started"
            )
            proposal = self.analyze_proposal(
                task.run_id, task_id=task.id, task_attempt=task.attempts
            )
            current_task = self._review_store.get_analysis_task(task.id)
            if current_task is not None and current_task.status == "cancelled":
                logger.bind(task_id=task.id, run_id=task.run_id).info(
                    "[learning] optimization analysis result discarded after cancellation"
                )
                continue
            if proposal.status == "ready":
                processed += 1
                logger.bind(task_id=task.id, run_id=task.run_id, attempt=task.attempts).info(
                    "[learning] optimization analysis completed"
                )
                continue
            updated = self._review_store.mark_analysis_task_failed(
                task.id,
                attempt=task.attempts,
                error_code=proposal.error_code or "AnalysisFailed",
                max_attempts=max_attempts,
            )
            logger.bind(
                task_id=task.id,
                run_id=task.run_id,
                attempt=task.attempts,
                status=updated.status,
                error_code=updated.error_code,
            ).warning("[learning] optimization analysis failed")
        return processed

    def list_candidates(self, *, limit: int = 200) -> list[CandidateCase]:
        return [
            self._with_candidate_question(candidate)
            for candidate in self._review_store.list_candidates(limit=limit)
        ]

    def _with_candidate_question(self, candidate: CandidateCase) -> CandidateCase:
        run = self._session_store.get_run(candidate.run_id)
        if run is None:
            return candidate
        return candidate.model_copy(update={"question": run.message[:20_000]})

    def candidate_lookup(self, run_ids: list[str]) -> dict[str, CandidateCase]:
        return {
            run_id: self._with_candidate_question(candidate)
            for run_id, candidate in self._review_store.get_candidates(run_ids).items()
        }

    def require_run(self, run_id: str) -> None:
        if self._session_store.get_run(run_id) is None:
            raise KeyError(run_id)

    def candidate(self, run_id: str) -> CandidateCase:
        candidate = self._review_store.get_candidate(run_id)
        if candidate is None or not candidate.active:
            raise KeyError(run_id)
        return candidate

    def decisions(self, run_id: str) -> list[CaseReviewDecision]:
        if self._review_store.get_candidate(run_id) is None:
            raise KeyError(run_id)
        return self._review_store.list_decisions(run_id)

    def decide(
        self,
        run_id: str,
        *,
        status: ReviewStatus,
        reviewer: str,
        reason: str | None,
    ) -> CandidateCase:
        self.candidate(run_id)
        if status == "approved":
            try:
                proposal = self.proposal(run_id)
            except KeyError as exc:
                raise ValueError("candidate analysis must be ready before approval") from exc
            if proposal.status != "ready":
                raise ValueError("candidate analysis must be ready before approval")
        now = datetime.now(UTC)
        snapshot = self._build_snapshot(run_id, created_at=now) if status == "approved" else None
        return self._review_store.record_decision(
            run_id=run_id,
            status=status,
            reviewer=reviewer,
            reason=reason,
            created_at=now,
            snapshot=snapshot,
        )

    def decide_gate_a(
        self,
        run_id: str,
        *,
        status: ReviewStatus,
        reviewer: str,
        reason: str | None,
    ) -> CandidateCase:
        if status not in {"approved", "rejected"}:
            raise ValueError("Gate A decision must approve or reject the run")
        self._require_terminal_run(run_id)
        proposal = self.proposal(run_id)
        if proposal.status != "ready":
            raise ValueError("run analysis must be ready before Gate A")
        candidate = self._review_store.get_candidate(run_id)
        if candidate is None or not candidate.active:
            self.add_manual_candidate(run_id, actor=reviewer)
        return self.decide(
            run_id,
            status=status,
            reviewer=reviewer,
            reason=reason,
        )

    def remove_candidate(self, run_id: str, *, reviewer: str, reason: str | None) -> CandidateCase:
        self.candidate(run_id)
        return self._review_store.remove_candidate(
            run_id=run_id,
            reviewer=reviewer,
            reason=reason,
            created_at=datetime.now(UTC),
        )

    def snapshot(self, run_id: str) -> ApprovedCaseSnapshot:
        snapshot = self._review_store.get_snapshot(run_id)
        if snapshot is None:
            raise KeyError(run_id)
        return snapshot

    def oracle_request(self, run_id: str) -> OracleRequest | None:
        return self._review_store.get_oracle_request(run_id)

    def start_material_research(self, run_id: str) -> OracleRequest:
        candidate = self.candidate(run_id)
        if candidate.status != "approved":
            raise ValueError("material research requires an approved candidate")
        task = self.skill_optimization_task(run_id)
        if task is not None and task.status in ACTIVE_SKILL_OPTIMIZATION_STATUSES:
            raise ValueError("skill optimization has already started")
        decision = next(
            (item for item in reversed(self.decisions(run_id)) if item.status == "approved"),
            None,
        )
        if decision is None:
            raise ValueError("material research requires an approved decision")
        return self._review_store.enqueue_oracle_request(
            snapshot=self.snapshot(run_id),
            decision=decision,
        )

    def save_reference_answer(self, run_id: str, *, answer: str | None) -> CandidateCase:
        candidate = self.candidate(run_id)
        if candidate.status != "approved":
            raise ValueError("reference answer requires an approved candidate")
        task = self.skill_optimization_task(run_id)
        if task is not None and task.status in ACTIVE_SKILL_OPTIMIZATION_STATUSES:
            raise ValueError("skill optimization has already started")
        normalized = answer.strip() if answer is not None else None
        return self._review_store.update_reference_answer(
            run_id=run_id,
            answer=normalized or None,
        )

    def skill_optimization_task(self, run_id: str) -> SkillOptimizationTask | None:
        return self._review_store.get_skill_optimization_task(run_id)

    def list_skill_optimizations(self, *, limit: int) -> list[SkillOptimizationTask]:
        return self._review_store.list_skill_optimization_tasks(limit=limit)

    def create_skill_optimization_experiment(
        self,
        *,
        run_ids: list[str],
        config: SkillOptimizationExperimentConfig,
        actor: str,
    ) -> SkillOptimizationExperiment:
        return self._review_store.create_skill_optimization_experiment(
            run_ids=run_ids,
            config=config,
            created_by=actor,
        )

    def skill_optimization_experiment(self, experiment_id: str) -> SkillOptimizationExperiment:
        experiment = self._review_store.get_skill_optimization_experiment(experiment_id)
        if experiment is None:
            raise KeyError(experiment_id)
        return experiment

    def list_skill_optimization_experiments(
        self, *, limit: int
    ) -> list[SkillOptimizationExperiment]:
        return self._review_store.list_skill_optimization_experiments(limit=limit)

    def skill_optimization_experiment_view(
        self, experiment_id: str
    ) -> SkillOptimizationExperimentView:
        return load_skill_optimization_experiment_view(
            experiment_root=self._skillopt_experiment_root,
            experiment=self.skill_optimization_experiment(experiment_id),
        )

    def review_skill_optimization_experiment(
        self, experiment_id: str, *, decision: str, reviewer: str, note: str | None
    ) -> SkillOptimizationExperiment:
        return self._review_store.review_skill_optimization_experiment(
            experiment_id, decision=decision, reviewer=reviewer, note=note
        )

    def skill_optimization_view(self, run_id: str) -> SkillOptimizationView:
        task = self.skill_optimization_task(run_id)
        if task is None:
            raise KeyError(run_id)
        if self._skillopt_experiment_root is None:
            return SkillOptimizationView(experiment_id=task.experiment_id, available=False)
        return load_skill_optimization_view(
            experiment_root=self._skillopt_experiment_root,
            task=task,
        )

    def enqueue_skill_optimization(self, run_id: str) -> SkillOptimizationTask:
        candidate = self.candidate(run_id)
        if candidate.status != "approved":
            raise ValueError("skill optimization requires an approved Gate A snapshot")
        self.snapshot(run_id)
        proposal = self.proposal(run_id)
        analysis = proposal.analysis
        if analysis is None:
            raise ValueError("skill optimization requires a completed analysis")
        request = self.oracle_request(run_id)
        use_accepted_materials = (
            request is not None
            and request.status in {"accepted", "revised"}
            and request.accepted_oracle_package is not None
        )
        return self._review_store.enqueue_skill_optimization_task(
            source_run_id=run_id,
            target_skill_id=self._target_skill_id,
            input_source="accepted_oracle" if use_accepted_materials else "gate_a_snapshot",
            oracle_request_id=request.id if use_accepted_materials else None,
        )

    def review_skill_optimization(
        self,
        run_id: str,
        *,
        decision: str,
        reviewer: str,
        note: str | None,
    ) -> SkillOptimizationTask:
        task = self.skill_optimization_task(run_id)
        if task is None:
            raise KeyError(run_id)
        return self._review_store.review_skill_optimization_task(
            task.id, decision=decision, reviewer=reviewer, note=note
        )

    def review_oracle_evidence(
        self,
        run_id: str,
        *,
        status: str,
        accepted_fact_ids: list[str],
        reviewer: str,
        note: str | None,
    ) -> OracleRequest:
        request = self.oracle_request(run_id)
        if request is None:
            raise KeyError(run_id)
        if status not in {"accepted", "revised", "rejected"}:
            raise ValueError("unsupported Gate B status")
        now = datetime.now(UTC)
        decision_ref = f"review://self-learning/oracle/{uuid4()}"
        review = OracleGateBReview(
            status=status,
            decision_ref=decision_ref,
            reviewer=reviewer,
            accepted_fact_ids=accepted_fact_ids,
            note=note,
            created_at=now,
        )
        accepted_package = None
        if status in {"accepted", "revised"}:
            if request.candidate_evidence_package is None:
                raise ValueError("Gate B review requires candidate evidence")
            candidate = CandidateEvidencePackage.model_validate(request.candidate_evidence_package)
            accepted_package = build_accepted_oracle_package(
                candidate=candidate,
                decision=GateBDecision(
                    status=status,
                    decision_ref=decision_ref,
                    accepted_fact_ids=accepted_fact_ids,
                ),
            ).model_dump(mode="json")
        return self._review_store.review_oracle_request(
            request.id,
            review=review,
            accepted_oracle_package=accepted_package,
        )

    def _build_snapshot(self, run_id: str, *, created_at: datetime) -> ApprovedCaseSnapshot:
        run = self._require_terminal_run(run_id)
        events = self._session_store.events_after(run_id, after_sequence=0)
        started = next(
            (event for event in events if event.type == AgentEventType.RUN_STARTED), None
        )
        completed = next(
            (event for event in reversed(events) if event.type == AgentEventType.MESSAGE_COMPLETED),
            None,
        )
        payload = completed.payload if completed is not None else {}
        proposal = self.proposal(run_id)
        analysis_task = self._review_store.latest_analysis_task(run_id)
        return ApprovedCaseSnapshot(
            run_id=run_id,
            question=run.message,
            answer=payload.get("text"),
            behavior_snapshot=(started.payload.get("behavior_snapshot", {}) if started else {}),
            citations=payload.get("citations") or {},
            source_event_sequences=[event.sequence for event in events],
            optimization_proposal=proposal,
            analysis_task_id=analysis_task.id if analysis_task is not None else None,
            analysis_attempts=analysis_task.attempts if analysis_task is not None else None,
            created_at=created_at,
        )

    def _require_terminal_run(self, run_id: str):
        run = self._session_store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}:
            raise ValueError(f"run {run_id} is not terminal")
        return run


def _public_analysis_error(exc: Exception) -> str:
    message = str(exc)
    if "runtime budget exhausted" in message and "recursion_steps" in message:
        match = re.search(r"recursion_step=(\d+)", message)
        if match is not None:
            return f"分析运行达到 {match.group(1)} 步上限，未能提交结果。"
        return "分析运行达到步数上限，未能提交结果。"
    if "runtime budget exhausted" in message and "elapsed_seconds" in message:
        return "分析运行达到时间上限，未能提交结果。"
    return f"分析执行失败（{type(exc).__name__}）。请查看服务日志。"
