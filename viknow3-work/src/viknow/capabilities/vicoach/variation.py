"""Per-request question perspectives for varied ViCoach practice sets."""

from __future__ import annotations

import secrets
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.capabilities.vicoach.domain import QuestionType, VideoChapter


class QuestionPerspective(StrEnum):
    """A fact-preserving perspective used to vary question construction."""

    CORE_FACT = "core_fact"
    CONCEPT_DISTINCTION = "concept_distinction"
    PROCESS_OR_CONDITION = "process_or_condition"
    CONTEXTUAL_APPLICATION = "contextual_application"
    NUMERIC_APPLICATION = "numeric_application"


class KnowledgePointVariation(BaseModel):
    """One required perspective for one source knowledge-point index."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_point_index: int = Field(ge=0)
    perspective: QuestionPerspective
    instruction: str = Field(min_length=1, max_length=1000)


class QuestionGenerationVariationPlan(BaseModel):
    """Private, random plan that diversifies one question-generation batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, max_length=64)
    knowledge_point_variations: list[KnowledgePointVariation]

    @model_validator(mode="after")
    def variations_must_cover_indexes_in_order(self) -> QuestionGenerationVariationPlan:
        indexes = [variation.knowledge_point_index for variation in self.knowledge_point_variations]
        if indexes != list(range(len(indexes))):
            raise ValueError("knowledge-point variations must use ascending contiguous indexes")
        return self


_PERSPECTIVE_INSTRUCTIONS: dict[QuestionPerspective, str] = {
    QuestionPerspective.CORE_FACT: (
        "考察该知识点最核心的定义、对象、构成或结论，不要直接照抄原句。"
    ),
    QuestionPerspective.CONCEPT_DISTINCTION: (
        "考察该知识点与章节内相关概念、边界或易混条件的辨析；仅使用章节已提供的事实。"
    ),
    QuestionPerspective.PROCESS_OR_CONDITION: (
        "考察该知识点中的步骤、因果、前提或条件变化；问题必须能由章节内容唯一作答。"
    ),
    QuestionPerspective.CONTEXTUAL_APPLICATION: (
        "结合章节描述中的人物、事件或环境构造简短应用情境，考察学习者如何运用该知识点；"
        "不能添加章节外的业务事实或规则。"
    ),
    QuestionPerspective.NUMERIC_APPLICATION: (
        "若知识点包含数值、比例、公式或可计算关系，在保持章节事实等价的前提下调整数值或"
        "条件并构造应用题；若不具备这些条件，改用核心事实或条件辨析视角。"
    ),
}


def build_question_generation_variation_plan(
    *,
    chapter: VideoChapter,
    question_type: QuestionType,
) -> QuestionGenerationVariationPlan:
    """Create a random, fact-bounded perspective for every chapter knowledge point."""

    random_source = secrets.SystemRandom()
    variations = []
    for index, knowledge_point in enumerate(chapter.knowledge_points):
        perspective = random_source.choice(
            _available_perspectives(
                knowledge_point=knowledge_point,
                question_type=question_type,
            )
        )
        variations.append(
            KnowledgePointVariation(
                knowledge_point_index=index,
                perspective=perspective,
                instruction=_PERSPECTIVE_INSTRUCTIONS[perspective],
            )
        )
    return QuestionGenerationVariationPlan(
        batch_id=secrets.token_hex(16),
        knowledge_point_variations=variations,
    )


def _available_perspectives(
    *,
    knowledge_point: str,
    question_type: QuestionType,
) -> tuple[QuestionPerspective, ...]:
    perspectives = (
        QuestionPerspective.CORE_FACT,
        QuestionPerspective.CONCEPT_DISTINCTION,
        QuestionPerspective.PROCESS_OR_CONDITION,
        QuestionPerspective.CONTEXTUAL_APPLICATION,
    )
    if question_type is QuestionType.MULTIPLE_CHOICE:
        perspectives = (
            QuestionPerspective.CONCEPT_DISTINCTION,
            QuestionPerspective.PROCESS_OR_CONDITION,
            QuestionPerspective.CONTEXTUAL_APPLICATION,
        )
    if any(character.isdigit() for character in knowledge_point):
        return (*perspectives, QuestionPerspective.NUMERIC_APPLICATION)
    return perspectives


__all__ = [
    "KnowledgePointVariation",
    "QuestionGenerationVariationPlan",
    "QuestionPerspective",
    "build_question_generation_variation_plan",
]
