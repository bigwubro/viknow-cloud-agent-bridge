"""Application service for durable, content-free Learning Projection."""

from __future__ import annotations

from viknow.core.learning.calibration import (
    CalibrationCandidate,
    select_calibration_candidates,
)
from viknow.core.learning.candidates import LearningCandidate, select_learning_candidates
from viknow.core.learning.observations import (
    LearningObservationReport,
    summarize_learning_candidates,
)
from viknow.core.learning.projection import LearningFeedback, project_learning_record
from viknow.core.learning.store import LearningProjectionStore
from viknow.core.runtime.domain import AgentEvent, AgentEventType
from viknow.core.session.domain import RunStatus
from viknow.core.session.store import SessionStore


class LearningProjectionService:
    """Projects terminal production-run facts through a durable idempotent outbox."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        learning_store: LearningProjectionStore,
        max_attempts: int = 0,
    ) -> None:
        self._session_store = session_store
        self._learning_store = learning_store
        self._max_attempts = max_attempts

    def enqueue_terminal_event(self, event: AgentEvent) -> bool:
        if event.type not in {
            AgentEventType.RUN_COMPLETED,
            AgentEventType.RUN_FAILED,
            AgentEventType.RUN_INTERRUPTED,
        }:
            return False
        return self.enqueue_completed_run(event.run_id)

    def enqueue_completed_run(self, run_id: str) -> bool:
        run = self._require_terminal_run(run_id)
        if _run_kind(self._session_store.events_after(run.id, after_sequence=0)) != "production":
            return False
        return self._learning_store.enqueue(run_id=run.id, feedback_revision=0)

    def submit_feedback(self, *, run_id: str, disposition: str) -> LearningFeedback:
        run = self._require_terminal_run(run_id)
        if _run_kind(self._session_store.events_after(run.id, after_sequence=0)) != "production":
            raise ValueError("evaluation runs cannot become production learning samples")
        feedback = self._learning_store.append_feedback(run_id=run.id, disposition=disposition)
        self._learning_store.enqueue(run_id=run.id, feedback_revision=feedback.revision)
        return feedback

    def process_pending_once(self, *, limit: int = 20) -> int:
        processed = 0
        for job in self._learning_store.claim_pending(limit=limit):
            try:
                events = self._session_store.events_after(job.run_id, after_sequence=0)
                if _run_kind(events) != "production":
                    raise ValueError("evaluation run found in learning projection outbox")
                feedback = self._learning_store.feedback_for_revision(
                    run_id=job.run_id,
                    revision=job.feedback_revision,
                )
                if job.feedback_revision != 0 and feedback is None:
                    raise ValueError(
                        f"feedback revision {job.feedback_revision} not found for run {job.run_id}"
                    )
                record = project_learning_record(events, feedback=feedback)
                self._learning_store.save_projection(record)
            except Exception as exc:
                self._learning_store.mark_job_failed(
                    job.key,
                    error=str(exc),
                    give_up=self._max_attempts > 0 and job.attempts >= self._max_attempts,
                )
                continue
            self._learning_store.mark_job_completed(job.key)
            processed += 1
        return processed

    def candidate_pool(self, *, limit: int = 200) -> list[LearningCandidate]:
        """Return deterministic review candidates; it never creates Oracle facts."""

        return select_learning_candidates(self._learning_store.list_projections(limit=limit))

    def candidate_observation_report(self, *, limit: int = 200) -> LearningObservationReport:
        """Summarize existing candidate signals without clustering user content."""

        return summarize_learning_candidates(self.candidate_pool(limit=limit))

    def calibration_candidates(self, *, limit: int = 50) -> list[CalibrationCandidate]:
        """Return pending, content-free review selections without recording a decision."""

        return select_calibration_candidates(
            self._learning_store.list_projections(limit=limit),
            limit=limit,
        )

    def _require_terminal_run(self, run_id: str):
        run = self._session_store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}:
            raise ValueError(f"run {run_id} is not terminal")
        return run


def _run_kind(events: list[AgentEvent]) -> str:
    for event in events:
        if event.type == AgentEventType.RUN_STARTED:
            value = event.payload.get("run_kind")
            return "evaluation" if value == "evaluation" else "production"
    return "production"
