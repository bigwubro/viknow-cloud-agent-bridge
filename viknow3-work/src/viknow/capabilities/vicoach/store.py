"""Persistence protocol for intelligent video training."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from viknow.capabilities.vicoach.domain import (
    AnswerAttempt,
    Assessment,
    InternalQuestionSnapshot,
)


class VicoachStore(Protocol):
    """Persistence boundary for owned assessments, question snapshots, and attempts."""

    def create_assessment(
        self,
        *,
        assessment: Assessment,
        questions: Sequence[InternalQuestionSnapshot],
    ) -> Assessment: ...

    def get_assessment_for_user(self, *, user_id: str, assessment_id: str) -> Assessment | None: ...

    def get_question_for_user(
        self, *, user_id: str, question_id: str
    ) -> InternalQuestionSnapshot | None: ...

    def list_questions_for_user(
        self, *, user_id: str, assessment_id: str
    ) -> list[InternalQuestionSnapshot]: ...

    def append_answer_attempt(self, *, user_id: str, attempt: AnswerAttempt) -> AnswerAttempt: ...

    def list_answer_attempts_for_user(
        self, *, user_id: str, assessment_id: str
    ) -> list[AnswerAttempt]: ...
