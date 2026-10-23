"""Explicit failures exposed by the ViCoach capability boundary."""

from __future__ import annotations


class VicoachError(Exception):
    """Base error for ViCoach capability operations."""


class InvalidChapterIdentifierError(VicoachError, ValueError):
    """A library or asset identifier cannot safely address a chapter file."""


class ChapterMetadataNotFoundError(VicoachError, FileNotFoundError):
    """The expected chapter metadata manifest does not exist."""


class ChapterMetadataReadError(VicoachError):
    """The chapter metadata manifest cannot be decoded as JSON."""


class ChapterMetadataValidationError(VicoachError, ValueError):
    """The chapter metadata manifest does not match the strict domain schema."""


class QuestionGenerationError(VicoachError):
    """A generator did not produce a valid, requested question set."""


class AnswerScoringError(VicoachError, ValueError):
    """A submitted answer cannot be scored for the supplied question."""


class LLMJudgeError(VicoachError):
    """The LLM judge did not return a structured score."""


__all__ = [
    "AnswerScoringError",
    "ChapterMetadataNotFoundError",
    "ChapterMetadataReadError",
    "ChapterMetadataValidationError",
    "InvalidChapterIdentifierError",
    "LLMJudgeError",
    "QuestionGenerationError",
    "VicoachError",
]
