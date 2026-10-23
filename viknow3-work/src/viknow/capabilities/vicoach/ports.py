"""Ports required by the ViCoach use-case service."""

from __future__ import annotations

from typing import Protocol

from viknow.capabilities.vicoach.domain import (
    FillBlankQuestion,
    LLMAnswerJudgement,
    OpenEndedQuestion,
    QuestionGenerationCandidate,
    QuestionGenerationRequest,
    VideoChapter,
    VideoChapterMetadata,
)
from viknow.capabilities.vicoach.variation import KnowledgePointVariation


class ChapterReader(Protocol):
    """Loads a validated chapter manifest from its owning storage boundary."""

    def read(self, *, library_id: str, asset_id: str) -> VideoChapterMetadata: ...


class QuestionGenerator(Protocol):
    """Generates one candidate decision for one selected knowledge point."""

    def generate(
        self,
        *,
        chapter: VideoChapter,
        request: QuestionGenerationRequest,
        knowledge_point_index: int,
        variation: KnowledgePointVariation,
    ) -> QuestionGenerationCandidate: ...


class FreeTextAnswerJudge(Protocol):
    """Scores fill-in and open-ended answers with their private references."""

    def judge(
        self,
        *,
        question: FillBlankQuestion | OpenEndedQuestion,
        answer: str,
    ) -> LLMAnswerJudgement: ...


__all__ = ["ChapterReader", "FreeTextAnswerJudge", "QuestionGenerator"]
