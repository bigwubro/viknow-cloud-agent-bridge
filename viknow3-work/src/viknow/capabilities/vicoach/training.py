"""Persistence-backed orchestration for a learner's ViCoach assessment."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter

from viknow.capabilities.vicoach.domain import (
    AnswerAttempt,
    Assessment,
    GeneratedQuestion,
    InternalQuestionSnapshot,
    PublicQuestion,
    QuestionGenerationRequest,
    QuestionGenerationResult,
    ScoreResult,
    VideoChapter,
    project_public_question,
    snapshots_from_generation_result,
)
from viknow.capabilities.vicoach.errors import QuestionGenerationError
from viknow.capabilities.vicoach.ports import ChapterReader
from viknow.capabilities.vicoach.service import QuestionGenerationProgress, VicoachService
from viknow.capabilities.vicoach.store import VicoachStore

_GENERATED_QUESTION = TypeAdapter(GeneratedQuestion)


class VicoachTrainingService:
    """Create, restore, and score persisted chapter-training assessments."""

    def __init__(
        self,
        *,
        service: VicoachService,
        chapter_reader: ChapterReader,
        store: VicoachStore,
    ) -> None:
        self._service = service
        self._chapter_reader = chapter_reader
        self._store = store

    def list_chapters(self, *, library_id: str, asset_id: str) -> list[VideoChapter]:
        return self._chapter_reader.read(library_id=library_id, asset_id=asset_id).chapters

    def create_assessment(
        self,
        *,
        user_id: str,
        request: QuestionGenerationRequest,
    ) -> tuple[Assessment, list[str]]:
        result = self._service.generate(request)
        return self._create_assessment_from_result(
            user_id=user_id,
            result=result,
        )

    def create_assessment_with_progress(
        self,
        *,
        user_id: str,
        request: QuestionGenerationRequest,
    ) -> Generator[QuestionGenerationProgress, None, tuple[Assessment, list[str]]]:
        """Create an assessment after yielding each completed generation step."""

        generation = self._service.generate_with_progress(request)
        while True:
            try:
                yield next(generation)
            except StopIteration as complete:
                return self._create_assessment_from_result(
                    user_id=user_id,
                    result=complete.value,
                )

    def _create_assessment_from_result(
        self,
        *,
        user_id: str,
        result: QuestionGenerationResult,
    ) -> tuple[Assessment, list[str]]:
        created_at = datetime.now(UTC)
        assessment = Assessment(
            id=str(uuid4()),
            user_id=user_id,
            library_id=result.library_id,
            asset_id=result.asset_id,
            chapter_order=result.chapter_order,
            question_type=result.question_type,
            created_at=created_at,
        )
        snapshots = snapshots_from_generation_result(
            assessment_id=assessment.id,
            result=result,
            created_at=created_at,
        )
        if not snapshots:
            raise QuestionGenerationError("所选章节没有适合该题型的知识点")
        self._store.create_assessment(assessment=assessment, questions=snapshots)
        return (
            assessment,
            [
                candidate.knowledge_point
                for candidate in result.candidates
                if candidate.status == "skipped"
            ],
        )

    def get_assessment(self, *, user_id: str, assessment_id: str) -> Assessment | None:
        return self._store.get_assessment_for_user(user_id=user_id, assessment_id=assessment_id)

    def list_questions(self, *, user_id: str, assessment_id: str) -> list[InternalQuestionSnapshot]:
        return self._store.list_questions_for_user(user_id=user_id, assessment_id=assessment_id)

    def submit_answer(
        self,
        *,
        user_id: str,
        assessment_id: str,
        question_id: str,
        answer: str | bool | list[str],
    ) -> AnswerAttempt:
        question = self._store.get_question_for_user(user_id=user_id, question_id=question_id)
        if question is None or question.assessment_id != assessment_id:
            raise KeyError(question_id)
        generated = _GENERATED_QUESTION.validate_python(question.internal_question_json)
        result = self._service.score(question=generated, answer=answer)
        return self._store.append_answer_attempt(
            user_id=user_id,
            attempt=AnswerAttempt(
                id=str(uuid4()),
                assessment_id=assessment_id,
                question_id=question_id,
                answer_json=answer,
                score=result.score,
                reason=result.feedback,
                created_at=datetime.now(UTC),
            ),
        )

    def list_attempts(self, *, user_id: str, assessment_id: str) -> list[AnswerAttempt]:
        return self._store.list_answer_attempts_for_user(
            user_id=user_id,
            assessment_id=assessment_id,
        )

    @staticmethod
    def public_question(snapshot: InternalQuestionSnapshot) -> PublicQuestion:
        question = _GENERATED_QUESTION.validate_python(snapshot.internal_question_json)
        return project_public_question(question)

    @staticmethod
    def score_result(attempt: AnswerAttempt, question: InternalQuestionSnapshot) -> ScoreResult:
        return ScoreResult(
            question_id=question.id,
            question_type=question.question_type,
            score=attempt.score if attempt.score is not None else 0,
            feedback=attempt.reason or "",
            judged_by="llm" if question.question_type.value >= 3 else "deterministic",
        )


__all__ = ["VicoachTrainingService"]
