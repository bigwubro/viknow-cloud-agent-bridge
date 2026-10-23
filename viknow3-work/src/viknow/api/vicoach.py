"""HTTP routes for persisted ViCoach video training."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from viknow.api.contracts import (
    VicoachAnswerAttemptResponse,
    VicoachAnswerSubmitRequest,
    VicoachAnswerSubmitResponse,
    VicoachAssessmentCreateRequest,
    VicoachAssessmentResponse,
    VicoachChapterLocationResponse,
    VicoachChapterResponse,
    VicoachChaptersResponse,
    VicoachOptionResponse,
    VicoachQuestionResponse,
)
from viknow.capabilities.vicoach.domain import (
    AnswerAttempt,
    Assessment,
    InternalQuestionSnapshot,
    QuestionGenerationRequest,
    QuestionType,
)
from viknow.capabilities.vicoach.errors import (
    AnswerScoringError,
    ChapterMetadataNotFoundError,
    ChapterMetadataReadError,
    ChapterMetadataValidationError,
    InvalidChapterIdentifierError,
    LLMJudgeError,
    QuestionGenerationError,
)
from viknow.capabilities.vicoach.ports import ChapterReader
from viknow.capabilities.vicoach.training import VicoachTrainingService


def create_vicoach_router(
    service_provider: Callable[[], VicoachTrainingService],
    *,
    chapter_reader_provider: Callable[[], ChapterReader] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/vicoach", tags=["vicoach"])

    @router.get(
        "/libraries/{library_id}/assets/{asset_id}/chapters",
        response_model=VicoachChaptersResponse,
    )
    def list_chapters(library_id: str, asset_id: str) -> VicoachChaptersResponse:
        try:
            chapters = (
                chapter_reader_provider().read(library_id=library_id, asset_id=asset_id).chapters
                if chapter_reader_provider is not None
                else service_provider().list_chapters(library_id=library_id, asset_id=asset_id)
            )
        except ChapterMetadataNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ChapterMetadataReadError,
            ChapterMetadataValidationError,
            InvalidChapterIdentifierError,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return VicoachChaptersResponse(
            library_id=library_id,
            asset_id=asset_id,
            chapters=[
                VicoachChapterResponse(
                    title=chapter.title,
                    description=chapter.description,
                    order=chapter.metadata.order,
                    location=VicoachChapterLocationResponse(
                        start=chapter.metadata.location.start,
                        end=chapter.metadata.location.end,
                    ),
                )
                for chapter in chapters
            ],
        )

    @router.post("/assessments", response_model=VicoachAssessmentResponse)
    def create_assessment(payload: VicoachAssessmentCreateRequest) -> VicoachAssessmentResponse:
        user_id = str(payload.user_id)
        service = service_provider()
        try:
            assessment, skipped_knowledge_points = service.create_assessment(
                user_id=user_id,
                request=_generation_request(payload),
            )
        except ChapterMetadataNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ChapterMetadataReadError,
            ChapterMetadataValidationError,
            InvalidChapterIdentifierError,
            QuestionGenerationError,
            LLMJudgeError,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _assessment_response(
            service=service,
            user_id=user_id,
            assessment=assessment,
            skipped_knowledge_points=skipped_knowledge_points,
        )

    @router.post("/assessments/stream")
    def create_assessment_stream(payload: VicoachAssessmentCreateRequest) -> StreamingResponse:
        """Create an assessment while reporting each completed knowledge point over SSE."""

        user_id = str(payload.user_id)
        service = service_provider()
        request = _generation_request(payload)

        def stream():
            try:
                generation = service.create_assessment_with_progress(
                    user_id=user_id,
                    request=request,
                )
                while True:
                    try:
                        progress = next(generation)
                    except StopIteration as complete:
                        assessment, skipped_knowledge_points = complete.value
                        response = _assessment_response(
                            service=service,
                            user_id=user_id,
                            assessment=assessment,
                            skipped_knowledge_points=skipped_knowledge_points,
                        )
                        yield _sse_event("complete", response.model_dump(mode="json"))
                        return
                    yield _sse_event("progress", progress.model_dump(mode="json"))
            except ChapterMetadataNotFoundError as exc:
                yield _sse_event("error", {"status": 404, "detail": str(exc)})
            except (
                ChapterMetadataReadError,
                ChapterMetadataValidationError,
                InvalidChapterIdentifierError,
                QuestionGenerationError,
                LLMJudgeError,
            ) as exc:
                yield _sse_event("error", {"status": 422, "detail": str(exc)})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/assessments/{assessment_id}", response_model=VicoachAssessmentResponse)
    def get_assessment(
        assessment_id: str,
        user_id: Annotated[UUID, Query()],
    ) -> VicoachAssessmentResponse:
        service = service_provider()
        assessment = service.get_assessment(user_id=str(user_id), assessment_id=assessment_id)
        if assessment is None:
            raise HTTPException(status_code=404, detail="assessment not found")
        return _assessment_response(service=service, user_id=str(user_id), assessment=assessment)

    @router.post(
        "/assessments/{assessment_id}/questions/{question_id}/answers",
        response_model=VicoachAnswerSubmitResponse,
    )
    def submit_answer(
        assessment_id: str,
        question_id: str,
        payload: VicoachAnswerSubmitRequest,
    ) -> VicoachAnswerSubmitResponse:
        user_id = str(payload.user_id)
        service = service_provider()
        if service.get_assessment(user_id=user_id, assessment_id=assessment_id) is None:
            raise HTTPException(status_code=404, detail="assessment not found")
        try:
            attempt = service.submit_answer(
                user_id=user_id,
                assessment_id=assessment_id,
                question_id=question_id,
                answer=payload.answer,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="question not found") from exc
        except (AnswerScoringError, LLMJudgeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        questions = service.list_questions(user_id=user_id, assessment_id=assessment_id)
        question_ids = [question.id for question in questions]
        index = question_ids.index(question_id)
        return VicoachAnswerSubmitResponse(
            attempt=_attempt_response(attempt),
            previous_question_id=question_ids[index - 1] if index else None,
            next_question_id=question_ids[index + 1] if index + 1 < len(question_ids) else None,
        )

    return router


def _generation_request(payload: VicoachAssessmentCreateRequest) -> QuestionGenerationRequest:
    return QuestionGenerationRequest(
        library_id=payload.library_id,
        asset_id=payload.asset_id,
        order=payload.order,
        question_type=QuestionType(payload.question_type),
    )


def _sse_event(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _assessment_response(
    *,
    service: VicoachTrainingService,
    user_id: str,
    assessment: Assessment,
    skipped_knowledge_points: list[str] | None = None,
) -> VicoachAssessmentResponse:
    questions = service.list_questions(user_id=user_id, assessment_id=assessment.id)
    attempts = service.list_attempts(user_id=user_id, assessment_id=assessment.id)
    return VicoachAssessmentResponse(
        assessment_id=assessment.id,
        library_id=assessment.library_id,
        asset_id=assessment.asset_id,
        order=assessment.chapter_order,
        question_type=int(assessment.question_type),
        created_at=assessment.created_at,
        questions=[_question_response(service, question) for question in questions],
        skipped_knowledge_points=skipped_knowledge_points or [],
        attempts=[_attempt_response(attempt) for attempt in attempts],
    )


def _question_response(
    service: VicoachTrainingService,
    snapshot: InternalQuestionSnapshot,
) -> VicoachQuestionResponse:
    question = service.public_question(snapshot)
    return VicoachQuestionResponse(
        question_id=question.id,
        knowledge_point=snapshot.knowledge_point,
        question_type=int(question.question_type),
        question_markdown=question.question_markdown,
        options=[
            VicoachOptionResponse(id=option.id, text=option.text) for option in question.options
        ],
    )


def _attempt_response(attempt: AnswerAttempt) -> VicoachAnswerAttemptResponse:
    return VicoachAnswerAttemptResponse(
        attempt_id=attempt.id,
        question_id=attempt.question_id,
        answer=attempt.answer_json,
        score=attempt.score if attempt.score is not None else 0,
        reason=attempt.reason or "",
        created_at=attempt.created_at,
    )


__all__ = ["create_vicoach_router"]
