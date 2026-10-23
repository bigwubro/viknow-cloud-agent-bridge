"""Structured-LLM adapters for the ViCoach generator and free-text judge."""

from __future__ import annotations

from viknow.capabilities.vicoach.domain import (
    FillBlankQuestion,
    LLMAnswerJudgement,
    OpenEndedQuestion,
    QuestionGenerationCandidate,
    QuestionGenerationRequest,
    VideoChapter,
)
from viknow.capabilities.vicoach.errors import LLMJudgeError, QuestionGenerationError
from viknow.capabilities.vicoach.prompts import (
    build_free_text_judging_prompt,
    build_question_generation_prompt,
)
from viknow.capabilities.vicoach.variation import KnowledgePointVariation
from viknow.core.multimodal.structured_llm import invoke_structured_llm


class StructuredLLMQuestionGenerator:
    """Question-generator adapter for a LangChain structured-output model."""

    def __init__(self, llm) -> None:
        self._llm = llm

    def generate(
        self,
        *,
        chapter: VideoChapter,
        request: QuestionGenerationRequest,
        knowledge_point_index: int,
        variation: KnowledgePointVariation,
    ) -> QuestionGenerationCandidate:
        result = invoke_structured_llm(
            self._llm,
            QuestionGenerationCandidate,
            build_question_generation_prompt(
                chapter=chapter,
                question_type=request.question_type,
                knowledge_point_index=knowledge_point_index,
                variation=variation,
            ),
            run_name="vicoach_question_generation",
            max_attempts=1,
            max_tokens=4096,
        )
        if result is None:
            raise QuestionGenerationError("question generator returned no structured result")
        return result


class StructuredLLMFreeTextAnswerJudge:
    """Free-text scoring adapter for a LangChain structured-output model."""

    def __init__(self, llm) -> None:
        self._llm = llm

    def judge(
        self,
        *,
        question: FillBlankQuestion | OpenEndedQuestion,
        answer: str,
    ) -> LLMAnswerJudgement:
        result = invoke_structured_llm(
            self._llm,
            LLMAnswerJudgement,
            build_free_text_judging_prompt(question=question, answer=answer),
            run_name="vicoach_free_text_judge",
            max_attempts=1,
            max_tokens=512,
        )
        if result is None:
            raise LLMJudgeError("free-text answer judge returned no structured result")
        return result


__all__ = ["StructuredLLMFreeTextAnswerJudge", "StructuredLLMQuestionGenerator"]
