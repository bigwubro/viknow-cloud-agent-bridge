"""Question generation and scoring use cases for ViCoach."""

from __future__ import annotations

import re
from collections.abc import Generator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.capabilities.vicoach.domain import (
    FillBlankQuestion,
    GeneratedQuestion,
    MultipleChoiceQuestion,
    OpenEndedQuestion,
    PublicQuestionGenerationResult,
    QuestionGenerationCandidate,
    QuestionGenerationRequest,
    QuestionGenerationResult,
    QuestionOption,
    ScoreResult,
    SingleChoiceQuestion,
    TrueFalseQuestion,
    VideoChapter,
    VideoChapterMetadata,
    project_public_generation_result,
)
from viknow.capabilities.vicoach.errors import AnswerScoringError, QuestionGenerationError
from viknow.capabilities.vicoach.ports import ChapterReader, FreeTextAnswerJudge, QuestionGenerator
from viknow.capabilities.vicoach.variation import (
    KnowledgePointVariation,
    build_question_generation_variation_plan,
)

_TRUE_ANSWERS = frozenset({"true", "t", "yes", "y", "1", "正确", "对", "是", "真"})
_FALSE_ANSWERS = frozenset({"false", "f", "no", "n", "0", "错误", "错", "否", "假"})
_SELECTION_SPLIT = re.compile(r"[,，、;；|/\n]+")


class VicoachService:
    """Coordinates selected-chapter generation and learner-answer scoring."""

    def __init__(
        self,
        *,
        chapter_reader: ChapterReader,
        question_generator: QuestionGenerator,
        free_text_judge: FreeTextAnswerJudge,
        question_generation_concurrency: int = 10,
    ) -> None:
        if question_generation_concurrency < 1:
            raise ValueError("question_generation_concurrency must be positive")
        self._chapter_reader = chapter_reader
        self._question_generator = question_generator
        self._free_text_judge = free_text_judge
        self._question_generation_concurrency = question_generation_concurrency

    def generate_with_progress(
        self, request: QuestionGenerationRequest
    ) -> Generator[QuestionGenerationProgress, None, QuestionGenerationResult]:
        """Generate candidates one by one and yield progress after each model call."""
        manifest = self._chapter_reader.read(
            library_id=request.library_id,
            asset_id=request.asset_id,
        )
        chapter = _find_chapter(manifest, order=request.order)
        variation_plan = build_question_generation_variation_plan(
            chapter=chapter,
            question_type=request.question_type,
        )
        variations = variation_plan.knowledge_point_variations
        candidates = {}
        variation_iterator = iter(variations)
        futures: dict[Future[QuestionGenerationCandidate], KnowledgePointVariation] = {}

        def submit_next(executor: ThreadPoolExecutor) -> bool:
            try:
                variation = next(variation_iterator)
            except StopIteration:
                return False
            futures[
                executor.submit(
                    self._question_generator.generate,
                    chapter=chapter,
                    request=request,
                    knowledge_point_index=variation.knowledge_point_index,
                    variation=variation,
                )
            ] = variation
            return True

        if variations:
            with ThreadPoolExecutor(
                max_workers=min(self._question_generation_concurrency, len(variations))
            ) as executor:
                while len(futures) < self._question_generation_concurrency and submit_next(
                    executor
                ):
                    pass
                while futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        variation = futures.pop(future)
                        candidate = self._validate_candidate(
                            future.result(),
                            chapter=chapter,
                            request=request,
                            variation=variation,
                        )
                        candidates[variation.knowledge_point_index] = candidate
                        yield QuestionGenerationProgress(
                            completed=len(candidates),
                            total=len(variations),
                        )
                        submit_next(executor)
        candidates = [
            candidates[index].model_copy(
                update={
                    "question": (
                        candidates[index].question.model_copy(update={"id": str(uuid4())})
                        if candidates[index].question is not None
                        else None
                    )
                }
            )
            for index in range(len(variations))
        ]
        return QuestionGenerationResult(
            library_id=request.library_id,
            asset_id=request.asset_id,
            chapter_order=request.order,
            question_type=request.question_type,
            candidates=candidates,
        )

    def generate(self, request: QuestionGenerationRequest) -> QuestionGenerationResult:
        """Generate all candidates without exposing intermediate progress."""

        generator = self.generate_with_progress(request)
        while True:
            try:
                next(generator)
            except StopIteration as complete:
                return complete.value

    def generate_public(self, request: QuestionGenerationRequest) -> PublicQuestionGenerationResult:
        """Generate a learner-safe snapshot without answer-bearing fields."""

        return project_public_generation_result(self.generate(request))

    def score(self, *, question: GeneratedQuestion, answer: str | bool | list[str]) -> ScoreResult:
        """Score standard UI answer values with deterministic or LLM-backed rules."""

        if isinstance(question, SingleChoiceQuestion):
            selected = _parse_selected_options(answer, question.options)
            score = 100.0 if selected == {question.correct_option_id} else 0.0
            return _deterministic_result(
                question,
                score,
                "回答正确。" if score == 100 else "回答错误。",
            )
        if isinstance(question, MultipleChoiceQuestion):
            selected = _parse_selected_options(answer, question.options)
            expected = set(question.correct_option_ids)
            overlap = len(expected & selected)
            score = 0.0
            if overlap:
                precision = overlap / len(selected)
                recall = overlap / len(expected)
                score = 200 * precision * recall / (precision + recall)
            return _deterministic_result(
                question,
                score,
                f"正确选项匹配的 F1 分数为 {score:.1f}。",
            )
        if isinstance(question, TrueFalseQuestion):
            parsed_answer = _parse_true_false(answer)
            score = 100.0 if parsed_answer is question.correct_answer else 0.0
            return _deterministic_result(
                question,
                score,
                "判断正确。" if score == 100 else "判断错误。",
            )
        if isinstance(question, (FillBlankQuestion, OpenEndedQuestion)):
            if not isinstance(answer, str):
                raise AnswerScoringError("free-text answer must be a string")
            judgement = self._free_text_judge.judge(question=question, answer=answer)
            return ScoreResult(
                question_id=question.id,
                question_type=question.question_type,
                score=judgement.score,
                feedback=judgement.feedback,
                judged_by="llm",
            )
        raise AnswerScoringError(f"unsupported question type: {question.question_type}")

    @staticmethod
    def _validate_candidate(
        generated: QuestionGenerationCandidate,
        *,
        chapter: VideoChapter,
        request: QuestionGenerationRequest,
        variation: KnowledgePointVariation,
    ) -> QuestionGenerationCandidate:
        if generated.knowledge_point_index != variation.knowledge_point_index:
            raise QuestionGenerationError(
                "question generator returned the wrong knowledge point index"
            )
        if generated.question is not None:
            if generated.question.question_type != request.question_type:
                raise QuestionGenerationError(
                    f"question for knowledge point {generated.knowledge_point!r} has an "
                    "unrequested question type"
                )
            if generated.question.chapter_order != request.order:
                raise QuestionGenerationError(
                    f"question for knowledge point {generated.knowledge_point!r} references "
                    "the wrong chapter order"
                )
        return generated.model_copy(
            update={"knowledge_point": chapter.knowledge_points[generated.knowledge_point_index]}
        )


class QuestionGenerationProgress(BaseModel):
    """One completed knowledge-point generation step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    completed: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def completed_must_not_exceed_total(self) -> QuestionGenerationProgress:
        if self.completed > self.total:
            raise ValueError("completed must not exceed total")
        return self


def _find_chapter(manifest: VideoChapterMetadata, *, order: int) -> VideoChapter:
    for chapter in manifest.chapters:
        if chapter.metadata.order == order:
            return chapter
    raise QuestionGenerationError(f"chapter order {order} does not exist in the asset manifest")


def _deterministic_result(
    question: SingleChoiceQuestion | MultipleChoiceQuestion | TrueFalseQuestion,
    score: float,
    feedback: str,
) -> ScoreResult:
    return ScoreResult(
        question_id=question.id,
        question_type=question.question_type,
        score=score,
        feedback=feedback,
        judged_by="deterministic",
    )


def _parse_selected_options(
    answer: str | bool | list[str], options: list[QuestionOption]
) -> set[str]:
    """Resolve common UI labels (``A``, ``A、C``) or option text to option ids."""

    if isinstance(answer, bool):
        raise AnswerScoringError("choice answer must be option label text or a list of labels")
    values = answer if isinstance(answer, list) else _SELECTION_SPLIT.split(answer)
    if not all(isinstance(value, str) for value in values):
        raise AnswerScoringError("choice answer values must be strings")
    selected: set[str] = set()
    for value in values:
        option_id = _resolve_option_id(value, options)
        if option_id is not None:
            selected.add(option_id)
    return selected


def _resolve_option_id(value: str, options: list[QuestionOption]) -> str | None:
    normalized = _normalize_choice(value)
    if not normalized:
        return None
    for option in options:
        if normalized == _normalize_choice(option.id) or normalized == _normalize_choice(
            option.text
        ):
            return option.id
    for option in options:
        escaped = re.escape(option.id)
        if re.match(
            rf"^\s*(?:选项|答案)?\s*[:：]?\s*{escaped}(?=$|[\s.)、，,;；])",
            value,
            flags=re.I,
        ):
            return option.id
        if re.search(rf"(?<![A-Za-z0-9_.-]){escaped}(?![A-Za-z0-9_.-])", value, flags=re.I):
            return option.id
    return None


def _normalize_choice(value: str) -> str:
    return re.sub(r"^(?:选项|答案)\s*[:：]?\s*", "", value.strip(), flags=re.I).strip("()[]{} .:：")


def _parse_true_false(answer: str | bool | list[str]) -> bool:
    if isinstance(answer, bool):
        return answer
    if not isinstance(answer, str):
        raise AnswerScoringError("true/false answer must be a boolean or recognized text")
    normalized = answer.strip().casefold()
    if normalized in _TRUE_ANSWERS:
        return True
    if normalized in _FALSE_ANSWERS:
        return False
    raise AnswerScoringError(f"unrecognized true/false answer: {answer!r}")


__all__ = ["VicoachService"]
