from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypedDict

EMPTY_KNOWLEDGEBASE_SUMMARY = "当前知识库里还没有任何已经索引好的文件，无法进行知识库查询与问答。"
DEFAULT_KNOWLEDGEBASE_SUMMARY_LIMIT = 1000

_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
_VIDEO_PARSE_MODES = {"video", "sop_video", "plain_video"}


class KnowledgebaseAssetSummaryItem(TypedDict):
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


def infer_asset_type_label(parse_mode: str | None, file_path: str | None) -> str:
    normalized_mode = normalize_text(parse_mode).lower()
    if normalized_mode in _VIDEO_PARSE_MODES:
        return "视频"
    suffix = Path(str(file_path or "")).suffix.lower()
    if suffix in _VIDEO_SUFFIXES:
        return "视频"
    return "文档"


def infer_asset_title(
    asset_id: str,
    file_path: str | None,
    title: str | None = None,
    file_data: dict[str, Any] | None = None,
) -> str:
    normalized_title = normalize_text(title)
    if normalized_title:
        return normalized_title
    if isinstance(file_data, dict):
        file_data_title = normalize_text(file_data.get("title"))
        if file_data_title:
            return file_data_title
        attributes = file_data.get("attributes")
        if isinstance(attributes, dict):
            for key in ("title", "name"):
                attribute_title = normalize_text(attributes.get(key))
                if attribute_title:
                    return attribute_title
    candidate = Path(str(file_path or "")).stem.strip()
    if candidate:
        return candidate
    normalized_asset_id = normalize_text(asset_id)
    if normalized_asset_id:
        return normalized_asset_id
    return "未命名文件"


def compose_summary_from_assets(
    asset_items: list[KnowledgebaseAssetSummaryItem],
    *,
    overview: str | None = None,
    max_chars: int = DEFAULT_KNOWLEDGEBASE_SUMMARY_LIMIT,
) -> str:
    if not asset_items:
        return EMPTY_KNOWLEDGEBASE_SUMMARY

    document_count = sum(1 for item in asset_items if item["type_label"] != "视频")
    video_count = sum(1 for item in asset_items if item["type_label"] == "视频")
    summary_overview = overview
    if not normalize_text(summary_overview):
        if len(asset_items) == 1:
            summary_overview = shorten_text(asset_items[0].get("summary") or "", 120)
        else:
            summary_overview = fallback_overview_from_assets(asset_items)

    return compose_knowledgebase_summary(
        document_count=document_count,
        video_count=video_count,
        overview=summary_overview or "",
        asset_lines=[
            build_asset_line(item["type_label"], item["title"], item["summary"])
            for item in asset_items
        ],
        max_chars=max_chars,
    )
