"""Domain contracts for chapter-grounded ViCoach practice questions."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionType(IntEnum):
    """Stable integer question types used by the ViCoach client contract."""

    SINGLE_CHOICE = 0
    MULTIPLE_CHOICE = 1
    TRUE_FALSE = 2
    FILL_BLANK = 3
    OPEN_ENDED = 4


class VideoLocation(BaseModel):
    """A chapter's time range in seconds within its source video."""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def end_must_follow_start(self) -> VideoLocation:
        if self.end <= self.start:
            raise ValueError("location.end must be greater than location.start")
        return self


class VideoChapterSourceMetadata(BaseModel):
    """The source-video identity and location attached to every chapter."""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, max_length=128)
    file_title: str = Field(min_length=1, max_length=500)
    order: int = Field(ge=1)
    modality: Literal["video"]
    location: VideoLocation
    source_event_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_file_tile(cls, value: object) -> object:
        if isinstance(value, dict) and "file_tile" in value:
            raise ValueError("metadata.file_tile is invalid; use metadata.file_title")
        return value


class VideoChapter(BaseModel):
    """The approved chapter object read from a per-asset chapter manifest."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)
    knowledge_points: list[str] = Field(default_factory=list)
    metadata: VideoChapterSourceMetadata

    @model_validator(mode="after")
    def knowledge_points_must_be_distinct_and_nonempty(self) -> VideoChapter:
        normalized = [point.strip() for point in self.knowledge_points]
        if any(not point for point in normalized):
            raise ValueError("knowledge_points must not contain blank values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("knowledge_points must be unique")
        return self


class VideoChapterDocumentMetadata(BaseModel):
    """Strict identity of the chapter manifest located below the files root."""

    model_config = ConfigDict(extra="forbid")

    library_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)


class VideoChapterMetadata(BaseModel):
    """Strict top-level chapter manifest: metadata plus approved chapter objects."""

    model_config = ConfigDict(extra="forbid")

    metadata: VideoChapterDocumentMetadata
    chapters: list[VideoChapter]

    @model_validator(mode="after")
    def chapter_orders_must_be_unique_and_ascending(self) -> VideoChapterMetadata:
        orders = [chapter.metadata.order for chapter in self.chapters]
        if len(orders) != len(set(orders)):
            raise ValueError("chapter metadata.order values must be unique")
        if orders != sorted(orders):
            raise ValueError("chapters must be ordered by metadata.order")
        if any(chapter.metadata.file_id != self.metadata.asset_id for chapter in self.chapters):
            raise ValueError("chapter metadata.file_id must match manifest metadata.asset_id")
        return self


class QuestionOption(BaseModel):
    """A selectable answer option, without correctness metadata."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=1000)


class _QuestionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    chapter_order: int = Field(ge=1)
    question_markdown: str = Field(min_length=1, max_length=8000)


class SingleChoiceQuestion(_QuestionBase):
    question_type: Literal[QuestionType.SINGLE_CHOICE]
    options: list[QuestionOption] = Field(min_length=2, max_length=8)
    correct_option_id: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def correct_option_must_exist(self) -> SingleChoiceQuestion:
        _validate_option_ids(self.options)
        if self.correct_option_id not in {option.id for option in self.options}:
            raise ValueError("correct_option_id must identify an option")
        return self


class MultipleChoiceQuestion(_QuestionBase):
    question_type: Literal[QuestionType.MULTIPLE_CHOICE]
    options: list[QuestionOption] = Field(min_length=3, max_length=8)
    correct_option_ids: list[str] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def correct_options_must_exist(self) -> MultipleChoiceQuestion:
        option_ids = _validate_option_ids(self.options)
        if len(self.correct_option_ids) != len(set(self.correct_option_ids)):
            raise ValueError("correct_option_ids must not contain duplicates")
        if not set(self.correct_option_ids).issubset(option_ids):
            raise ValueError("each correct_option_id must identify an option")
        return self


class TrueFalseQuestion(_QuestionBase):
    question_type: Literal[QuestionType.TRUE_FALSE]
    correct_answer: bool


class FillBlankQuestion(_QuestionBase):
    question_type: Literal[QuestionType.FILL_BLANK]
    accepted_answers: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def accepted_answers_must_be_nonempty(self) -> FillBlankQuestion:
        if any(not answer.strip() for answer in self.accepted_answers):
            raise ValueError("accepted_answers must not contain blank values")
        return self


class OpenEndedQuestion(_QuestionBase):
    question_type: Literal[QuestionType.OPEN_ENDED]
    reference_answer: str = Field(min_length=1, max_length=8000)
    rubric: str = Field(min_length=1, max_length=4000)


GeneratedQuestion = Annotated[
    SingleChoiceQuestion
    | MultipleChoiceQuestion
    | TrueFalseQuestion
    | FillBlankQuestion
    | OpenEndedQuestion,
    Field(discriminator="question_type"),
]


class QuestionGenerationCandidate(BaseModel):
    """One generation decision for one original chapter knowledge point."""

    model_config = ConfigDict(extra="forbid")

    knowledge_point_index: int = Field(ge=0)
    knowledge_point: str = ""
    status: Literal["generated", "skipped"]
    question: GeneratedQuestion | None
    skip_reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_a_question_or_skip_reason(self) -> QuestionGenerationCandidate:
        if self.status == "generated":
            if self.question is None:
                raise ValueError("generated candidates require a question")
            if self.skip_reason:
                raise ValueError("generated candidates must not include a skip_reason")
        elif self.question is not None:
            raise ValueError("skipped candidates must not include a question")
        elif not self.skip_reason.strip():
            raise ValueError("skipped candidates require a skip_reason")
        return self


class QuestionGenerationCandidates(BaseModel):
    """Private structured-LLM output containing one result for each knowledge point."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[QuestionGenerationCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def generated_question_ids_must_be_unique(self) -> QuestionGenerationCandidates:
        ids = [
            candidate.question.id for candidate in self.candidates if candidate.question is not None
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("generated question ids must be unique")
        return self


class QuestionGenerationResult(BaseModel):
    """Snapshot-ready private generation result for one selected chapter and type."""

    model_config = ConfigDict(extra="forbid")

    library_id: str
    asset_id: str
    chapter_order: int = Field(ge=1)
    question_type: QuestionType
    candidates: list[QuestionGenerationCandidate] = Field(default_factory=list)


class PublicQuestion(BaseModel):
    """Question representation safe to return to learners."""

    model_config = ConfigDict(extra="forbid")

    id: str
    chapter_order: int
    question_type: QuestionType
    question_markdown: str
    options: list[QuestionOption] = Field(default_factory=list)


class PublicQuestionGenerationCandidate(BaseModel):
    """Learner-safe record of a generated or skipped knowledge point."""

    model_config = ConfigDict(extra="forbid")

    knowledge_point: str
    status: Literal["generated", "skipped"]
    question: PublicQuestion | None = None
    skip_reason: str = ""


class PublicQuestionGenerationResult(BaseModel):
    """Snapshot-ready generation result without answer-bearing fields."""

    model_config = ConfigDict(extra="forbid")

    library_id: str
    asset_id: str
    chapter_order: int
    question_type: QuestionType
    candidates: list[PublicQuestionGenerationCandidate] = Field(default_factory=list)


class QuestionGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    order: int = Field(ge=1)
    question_type: QuestionType


class ScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    question_type: QuestionType
    score: float = Field(ge=0, le=100)
    feedback: str = ""
    judged_by: Literal["deterministic", "llm"]


class Assessment(BaseModel):
    """An owned assessment generated for one chapter of a library video."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    library_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    chapter_order: int = Field(ge=1)
    question_type: QuestionType
    created_at: datetime


class InternalQuestionSnapshot(BaseModel):
    """Private question material, including answer keys, for an assessment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    assessment_id: str = Field(min_length=1, max_length=128)
    candidate_order: int = Field(ge=0)
    knowledge_point: str = Field(min_length=1)
    question_type: QuestionType
    question_markdown: str = Field(min_length=1, max_length=8000)
    internal_question_json: dict[str, Any]
    created_at: datetime


class AnswerAttempt(BaseModel):
    """An immutable, append-only learner response and its scoring result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    assessment_id: str = Field(min_length=1, max_length=128)
    question_id: str = Field(min_length=1, max_length=128)
    answer_json: str | bool | list[str]
    score: float | None = Field(default=None, ge=0, le=100)
    reason: str | None = Field(default=None, max_length=4000)
    created_at: datetime


def snapshots_from_generation_result(
    *,
    assessment_id: str,
    result: QuestionGenerationResult,
    created_at: datetime,
) -> list[InternalQuestionSnapshot]:
    """Snapshot generated candidates while retaining their original candidate order."""

    return [
        InternalQuestionSnapshot(
            id=candidate.question.id,
            assessment_id=assessment_id,
            candidate_order=candidate_order,
            knowledge_point=candidate.knowledge_point,
            question_type=candidate.question.question_type,
            question_markdown=candidate.question.question_markdown,
            internal_question_json=candidate.question.model_dump(mode="json"),
            created_at=created_at,
        )
        for candidate_order, candidate in enumerate(result.candidates)
        if candidate.status == "generated" and candidate.question is not None
    ]


class LLMAnswerJudgement(BaseModel):
    """Structured result from the LLM judge for free-text questions."""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=100)
    feedback: str = Field(default="", max_length=4000)


def project_public_question(question: GeneratedQuestion) -> PublicQuestion:
    """Drop every answer-bearing field before a question leaves the domain."""

    options = (
        list(question.options)
        if isinstance(question, (SingleChoiceQuestion, MultipleChoiceQuestion))
        else []
    )
    return PublicQuestion(
        id=question.id,
        chapter_order=question.chapter_order,
        question_type=question.question_type,
        question_markdown=question.question_markdown,
        options=options,
    )


def project_public_generation_result(
    result: QuestionGenerationResult,
) -> PublicQuestionGenerationResult:
    """Project a complete generation snapshot without exposing answer keys."""

    return PublicQuestionGenerationResult(
        library_id=result.library_id,
        asset_id=result.asset_id,
        chapter_order=result.chapter_order,
        question_type=result.question_type,
        candidates=[
            PublicQuestionGenerationCandidate(
                knowledge_point=candidate.knowledge_point,
                status=candidate.status,
                question=(
                    project_public_question(candidate.question)
                    if candidate.question is not None
                    else None
                ),
                skip_reason=candidate.skip_reason,
            )
            for candidate in result.candidates
        ],
    )


def _validate_option_ids(options: list[QuestionOption]) -> set[str]:
    option_ids = {option.id for option in options}
    if len(option_ids) != len(options):
        raise ValueError("option ids must be unique")
    return option_ids


__all__ = [
    "AnswerAttempt",
    "Assessment",
    "FillBlankQuestion",
    "GeneratedQuestion",
    "InternalQuestionSnapshot",
    "LLMAnswerJudgement",
    "MultipleChoiceQuestion",
    "OpenEndedQuestion",
    "PublicQuestion",
    "PublicQuestionGenerationCandidate",
    "PublicQuestionGenerationResult",
    "QuestionGenerationCandidate",
    "QuestionGenerationCandidates",
    "QuestionGenerationRequest",
    "QuestionGenerationResult",
    "QuestionOption",
    "QuestionType",
    "ScoreResult",
    "SingleChoiceQuestion",
    "TrueFalseQuestion",
    "VideoChapter",
    "VideoChapterDocumentMetadata",
    "VideoChapterMetadata",
    "VideoChapterSourceMetadata",
    "VideoLocation",
    "project_public_generation_result",
    "project_public_question",
    "snapshots_from_generation_result",
]
