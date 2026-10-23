"""Persistence boundary for content-free Learning Projection records."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.learning.projection import LearningFeedback, LearningProjectionRecord

LearningJobStatus = Literal["pending", "processing", "completed", "failed"]


class LearningProjectionJob(BaseModel):
    """Durable outbox item for one immutable projection identity."""

    model_config = ConfigDict(extra="forbid")

    key: str
    run_id: str
    feedback_revision: int = Field(ge=0)
    status: LearningJobStatus
    attempts: int = Field(ge=0)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class LearningProjectionStore(Protocol):
    """Store projections, feedback revisions, and their durable outbox."""

    def enqueue(self, *, run_id: str, feedback_revision: int) -> bool: ...

    def claim_pending(self, *, limit: int) -> list[LearningProjectionJob]: ...

    def mark_job_completed(self, key: str) -> None: ...

    def mark_job_failed(self, key: str, *, error: str, give_up: bool) -> None: ...

    def pending_jobs(self) -> list[LearningProjectionJob]: ...

    def save_projection(self, record: LearningProjectionRecord) -> bool: ...

    def get_projection(self, storage_key: str) -> LearningProjectionRecord | None: ...

    def list_projections(self, *, limit: int) -> list[LearningProjectionRecord]: ...

    def append_feedback(self, *, run_id: str, disposition: str) -> LearningFeedback: ...

    def feedback_for_revision(self, *, run_id: str, revision: int) -> LearningFeedback | None: ...
