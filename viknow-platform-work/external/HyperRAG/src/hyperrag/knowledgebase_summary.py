from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict


EMPTY_KNOWLEDGEBASE_SUMMARY = "当前知识库里还没有任何已经索引好的文件，无法进行知识库查询与问答。"
DEFAULT_KNOWLEDGEBASE_SUMMARY_LIMIT = 8000


class KnowledgebaseAssetSummaryItem(TypedDict):
    asset_id: str
    type_label: str
    title: str
    summary: str


def normalize_text(text: str | None) -> str:
    if text is None:
        return ""
    return " ".join(str(text).replace("\r", " ").replace("\n", " ").split()).strip()


def shorten_text(text: str | None, max_chars: int, suffix: str = "...") -> str:
    normalized = normalize_text(text)
    if max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return normalized[: max_chars - len(suffix)].rstrip() + suffix


def build_count_line(document_count: int, video_count: int) -> str:
    return f"当前知识库已索引完成{int(document_count)}个文档、{int(video_count)}个视频。"


def build_overview_line(overview: str) -> str:
    normalized = normalize_text(overview)
    return f"整体概括：{normalized}" if normalized else "整体概括：知识库已收录多份可检索内容。"


def build_asset_line(type_label: str, title: str, summary: str) -> str:
    normalized_type = normalize_text(type_label) or "文件"
    normalized_title = normalize_text(title) or "未命名文件"
    normalized_summary = normalize_text(summary) or "已完成索引，可用于知识库检索与问答。"
    return f"【{normalized_type}】《{normalized_title}》：{normalized_summary}"


def compose_knowledgebase_summary(
    *,
    document_count: int,
    video_count: int,
    overview: str,
    asset_lines: Iterable[str],
    max_chars: int = DEFAULT_KNOWLEDGEBASE_SUMMARY_LIMIT,
) -> str:
    count_line = build_count_line(document_count, video_count)
    overview_line = build_overview_line(shorten_text(overview, 140))
    base_parts: list[str] = [count_line, overview_line]
    base_text = "\n".join(base_parts)

    if len(base_text) >= max_chars:
        return base_text[:max_chars]

    detail_lines = [normalize_text(line) for line in asset_lines if normalize_text(line)]
    if not detail_lines:
        return base_text

    output_parts = list(base_parts)
    detail_header = "逐项摘要："
    if len(base_text) + 1 + len(detail_header) < max_chars:
        output_parts.append(detail_header)

    for index, line in enumerate(detail_lines, start=1):
        candidate = f"{index}. {line}"
        new_text = "\n".join(output_parts + [candidate])
        if len(new_text) > max_chars:
            break
        output_parts.append(candidate)

    return "\n".join(output_parts)


def fallback_overview_from_assets(asset_items: list[KnowledgebaseAssetSummaryItem]) -> str:
    titles = [item["title"] for item in asset_items if item.get("title")]
    if not titles:
        return "知识库已收录多份可检索内容，可围绕其中的流程、概念和细节发问。"
    preview = "、".join(titles[:3])
    return shorten_text(
        f"知识库当前主要覆盖{preview}等内容，可围绕相关流程、概念、步骤和细节继续提问。",
        120,
    )
