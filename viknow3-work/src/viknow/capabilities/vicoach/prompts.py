"""Prompt construction for structured ViCoach question generation and judging."""

from __future__ import annotations

import json

from viknow.capabilities.vicoach.domain import (
    FillBlankQuestion,
    OpenEndedQuestion,
    QuestionType,
    VideoChapter,
)
from viknow.capabilities.vicoach.variation import KnowledgePointVariation


def build_question_generation_prompt(
    *,
    chapter: VideoChapter,
    question_type: QuestionType,
    knowledge_point_index: int,
    variation: KnowledgePointVariation,
) -> str:
    """Build a one-knowledge-point prompt for the structured candidate schema."""

    chapter_payload = json.dumps(chapter.model_dump(mode="json"), ensure_ascii=False)
    type_instruction = _QUESTION_TYPE_PROMPTS[question_type]
    return (
        "你是 ViCoach 视频学习题生成器。只根据提供的视频章节内容出题，不能补充外部事实。"
        "输出必须严格符合结构化 schema，不要输出 Markdown 或解释。\n"
        f"目标题型只能是 {question_type.value} ({question_type.name.lower()})。\n"
        f"本题型出题规则：{type_instruction}\n"
        "只处理指定 knowledge_point_index 的一个原始知识点：适合出题时 status=generated 并"
        "生成一道目标题型问题；不适合时 status=skipped，并写明 skip_reason。"
        f"knowledge_point_index 必须为 {knowledge_point_index}，不得改写或使用其他索引。"
        "不要回传 knowledge_point 正文，服务端会根据 knowledge_point_index 关联原始文本。"
        "必须包含 question 字段：status=generated 时填完整题目对象；"
        "status=skipped 时填 null。"
        "generated question 的 chapter_order 必须等于该章节 metadata.order，id 必须唯一。"
        "选项不可通过措辞泄露正确答案，question_markdown 必须是 Markdown 且题目可由章节"
        "内容直接作答。"
        "必须按当前知识点的变体指令设计题目：优先从指定的考察视角"
        "选择事实侧重、章节情境或等价条件；不得直接复述知识点，不得引入章节外事实，"
        "不得让题目存在多个合理答案。\n"
        f"当前知识点索引：{knowledge_point_index}\n"
        f"当前知识点：{chapter.knowledge_points[knowledge_point_index]}\n"
        f"当前变体指令：{variation.instruction}\n"
        f"选定章节：{chapter_payload}"
    )


def build_free_text_judging_prompt(
    *,
    question: FillBlankQuestion | OpenEndedQuestion,
    answer: str,
) -> str:
    """Build the prompt for scoring an answer that cannot be deterministically graded."""

    if isinstance(question, FillBlankQuestion):
        reference = {"accepted_answers": question.accepted_answers}
        guidance = "忽略大小写、首尾空白及不改变语义的标点差异；部分正确可以给部分分。"
    else:
        reference = {
            "reference_answer": question.reference_answer,
            "rubric": question.rubric,
        }
        guidance = "按 rubric 和参考答案打分；不要要求逐字匹配。"
    return (
        "你是 ViCoach 学习答案评阅器。只基于题目和私有参考答案评分，"
        "输出必须符合结构化 schema。score 是 0 至 100，feedback 用简短中文说明。\n"
        f"评分规则：{guidance}\n"
        f"题目：{question.question_markdown}\n"
        f"私有参考：{json.dumps(reference, ensure_ascii=False)}\n"
        f"学习者答案：{answer}"
    )


def question_type_label(question_type: QuestionType) -> str:
    return {
        QuestionType.SINGLE_CHOICE: "单选题",
        QuestionType.MULTIPLE_CHOICE: "多选题",
        QuestionType.TRUE_FALSE: "判断题",
        QuestionType.FILL_BLANK: "填空题",
        QuestionType.OPEN_ENDED: "问答题",
    }[question_type]


_QUESTION_TYPE_PROMPTS: dict[QuestionType, str] = {
    QuestionType.SINGLE_CHOICE: (
        "生成单选题：提供 2 至 8 个 options 和唯一 correct_option_id。干扰项必须与章节语境相关，"
        "但不可暗示正确答案。"
    ),
    QuestionType.MULTIPLE_CHOICE: (
        "生成多选题：提供 3 至 8 个 options 和至少 2 个 correct_option_ids。仅在知识点可以自然"
        "区分多个正确要素时出题；否则跳过。"
    ),
    QuestionType.TRUE_FALSE: (
        "生成判断题：题干必须是可明确判定的陈述，correct_answer 必须为 boolean。避免含糊、"
        "多条件或依赖外部常识的表述。"
    ),
    QuestionType.FILL_BLANK: (
        "生成填空题：题干使用 Markdown 的 `____` 明确标出一个或多个空位，accepted_answers"
        "列出可接受的等义答案。"
    ),
    QuestionType.OPEN_ENDED: (
        "生成问答题：题干要求解释、归纳或说明章节知识；reference_answer 给出事实正确的参考答案，"
        "rubric 列出可评分的关键要点。"
    ),
}


__all__ = [
    "build_free_text_judging_prompt",
    "build_question_generation_prompt",
    "question_type_label",
]
