"""Batch review orchestration for selected indexed documents."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .models import IndexQualityReport, ReviewStatus


@dataclass(frozen=True)
class BatchReviewItem:
    workspace: str
    asset_id: str


def load_batch_review_items(csv_path: Path, *, default_workspace: str) -> list[BatchReviewItem]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV file does not exist: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError("CSV file is missing a header row")

        header = {str(name).strip().lower() for name in reader.fieldnames if name is not None}
        if "doc_id" not in header:
            raise ValueError("CSV file must contain a doc_id column")

        items: list[BatchReviewItem] = []
        for row in reader:
            asset_id = str((row.get("doc_id") or row.get("asset_id") or "")).strip()
            if not asset_id:
                continue
            workspace = str((row.get("workspace") or default_workspace or "")).strip()
            if not workspace:
                raise ValueError(f"CSV row for doc_id={asset_id} is missing workspace")
            items.append(BatchReviewItem(workspace=workspace, asset_id=asset_id))

    if not items:
        raise ValueError("CSV file did not contain any doc_id rows")
    return items


def render_batch_summary(reports: list[IndexQualityReport], *, source_csv: Path) -> str:
    if not reports:
        raise ValueError("No reports were generated")

    lines: list[str] = [
        "# 索引质量审查批量汇总",
        "",
        f"- CSV: `{source_csv}`",
        f"- 文档数: `{len(reports)}`",
        "",
        "## 1. 文档指标汇总",
        "",
        "| 文档 | asset_id | 信息留存得分 | 平均语义完整性得分 | chunk 加权语义完整性 | 总体状态 |",
        "|---|---|---:|---:|---:|---|",
    ]

    for report in reports:
        lines.append(
            "| "
            f"{_display_report_name(report)} | {report.asset_id} | "
            f"{_fmt(report.parse_review.information_retention)} | "
            f"{_fmt(report.chunk_macro_score)} | "
            f"{_fmt(report.chunk_token_weighted_score)} | {report.status.value} |"
        )

    lines.extend([
        "",
        "### 1.1 逐篇解读",
        "",
    ])
    for report in reports:
        lines.append(f"- {_display_report_name(report)}：{_summarize_report(report)}")

    lines.extend([
        "",
        "## 2. Bad Case 分类",
        "",
    ])

    parse_counter: Counter[str] = Counter()
    chunk_counter: Counter[str] = Counter()
    for report in reports:
        parse_counter.update(_classify_parse_case(report))
        chunk_counter.update(_classify_chunk_case(report))

    lines.extend(_render_counter_section("解析侧 bad case", parse_counter))
    lines.extend(_render_counter_section("分块侧 bad case", chunk_counter))

    lines.extend([
        "",
        "## 3. 优化建议",
        "",
    ])
    lines.extend(_render_optimization_suggestions())

    lines.extend([
        "",
        "## 4. 附录：逐文档 bad case 详情",
        "",
    ])
    for report in reports:
        lines.extend(_render_report_appendix(report))
    return "\n".join(lines).rstrip() + "\n"


def _render_counter_section(title: str, counter: Counter[str]) -> list[str]:
    lines = [f"### {title}", ""]
    if not counter:
        lines.append("- 无")
        return lines
    for key, value in counter.most_common():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.extend(_render_counter_explanations(title, counter))
    return lines


def _render_report_appendix(report: IndexQualityReport, *, bad_case_limit: int = 20) -> list[str]:
    lines = [
        f"### doc_id: {report.asset_id}",
        "",
        f"- Workspace: `{report.workspace}`",
        f"- Source: `{report.source_path}`",
        f"- Status: **{report.status.value}**",
        f"- Information retention: **{_fmt(report.parse_review.information_retention)}**",
        f"- Chunk semantic completeness: **{_fmt(report.chunk_macro_score)}**",
        "",
        "#### Parse Bad Cases",
        "",
    ]

    parse_bad_cases = sorted(
        (
            finding
            for finding in report.parse_review.findings
            if finding.status != ReviewStatus.PASS
        ),
        key=lambda finding: (finding.status != ReviewStatus.FAIL, finding.score),
    )[:bad_case_limit]
    if not parse_bad_cases:
        lines.append("No parse bad cases found.")
    for finding in parse_bad_cases:
        excerpt = _single_line(finding.source_unit.content, 180)
        lines.extend(
            [
                f"##### {finding.status.value.upper()} {finding.source_unit.unit_id}",
                "",
                f"- Type: `{finding.source_unit.kind}`",
                f"- Score: `{finding.score:.3f}`",
                f"- Location: `{json.dumps(finding.source_unit.locator, ensure_ascii=False)}`",
                f"- Reason: {finding.reason}",
                f"- Source excerpt: {excerpt}",
                "",
            ]
        )

    lines.extend(["#### Chunk Bad Cases", ""])
    chunk_bad_cases = sorted(
        (review for review in report.chunk_reviews if review.status != ReviewStatus.PASS),
        key=lambda review: (
            review.status != ReviewStatus.FAIL,
            review.semantic_score if review.semantic_score is not None else -1.0,
        ),
    )[:bad_case_limit]
    if not chunk_bad_cases:
        lines.append("No chunk bad cases found.")
    for review in chunk_bad_cases:
        reason = review.judgment.reason if review.judgment else review.error or "unreviewed"
        rule_flags = ", ".join(item.code for item in review.boundary_analysis.findings)
        lines.extend(
            [
                f"##### {review.status.value.upper()} {review.chunk.chunk_id}",
                "",
                f"- Order: `{review.chunk.order}`",
                f"- Score: `{_format_score(review.semantic_score)}`",
                f"- Rule flags: `{rule_flags}`",
                f"- Reason: {reason}",
                f"- Content excerpt: {_single_line(review.chunk.content, 260)}",
                "",
            ]
        )
    return lines


def _classify_parse_case(report: IndexQualityReport) -> Counter[str]:
    counter: Counter[str] = Counter()
    for finding in report.parse_review.findings:
        if finding.status == ReviewStatus.PASS:
            continue
        text = (finding.reason or "").lower()
        if any(token in text for token in ("视觉", "visual", "图", "表格")):
            if any(token in text for token in ("扩写", "generated", "视觉要点")):
                counter["visual-generated-noise"] += 1
            else:
                counter["visual-figure-loss"] += 1
        elif any(token in text for token in ("表格", "table")):
            counter["table-loss"] += 1
        elif any(token in text for token in ("目录", "页脚", "metadata", "元数据", "标题")):
            counter["metadata-loss"] += 1
        elif any(token in text for token in ("术语", "定义")):
            counter["glossary-loss"] += 1
        elif any(token in text for token in ("业务", "流程", "workflow")):
            counter["procedural-detail-loss"] += 1
        else:
            counter["other"] += 1
    return counter


def _classify_chunk_case(report: IndexQualityReport) -> Counter[str]:
    counter: Counter[str] = Counter()
    for review in report.chunk_reviews:
        if review.status == ReviewStatus.PASS:
            continue
        reason = (review.judgment.reason if review.judgment else review.error or review.boundary_analysis.status.value).lower()
        if review.status == ReviewStatus.UNREVIEWED:
            counter["unreviewed"] += 1
        elif any(token in reason for token in ("boundary", "边界", "截断", "拼接")):
            counter["boundary-issue"] += 1
        elif any(token in reason for token in ("semantic", "语义", "coverage", "完整性")):
            counter["semantic-loss"] += 1
        else:
            counter["other"] += 1
    return counter


def _fmt(value: float | None) -> str:
    return "unreviewed" if value is None else f"{value:.3f}"


def _format_score(score: float | None) -> str:
    return "unreviewed" if score is None else f"{score:.3f}"


def _single_line(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _display_report_name(report: IndexQualityReport) -> str:
    suffix = Path(report.source_path).suffix.lower()
    prefix = {
        ".pdf": "PDF",
        ".doc": "DOC",
        ".docx": "DOCX",
        ".md": "Markdown",
        ".ppt": "PPT",
        ".pptx": "PPTX",
        ".xls": "XLS",
        ".xlsx": "XLSX",
    }.get(suffix, "文档")
    return f"{prefix}：{Path(report.source_path).stem}"


def _summarize_report(report: IndexQualityReport) -> str:
    if report.parse_review.information_retention is None and report.chunk_macro_score is None:
        return "当前审查仍在进行，指标尚未完全产出。"
    if report.parse_review.information_retention is None:
        return "信息留存尚未完成，但 chunk 侧已经暴露出边界或完整性问题。"
    if report.parse_review.information_retention >= 0.9 and (report.chunk_macro_score or 0.0) < 0.65:
        return "信息留存较好，主要短板在 chunk 边界与可独立阅读性。"
    if report.parse_review.information_retention >= 0.75 and (report.chunk_macro_score or 0.0) < 0.7:
        return "信息留存已恢复到可用区间，但 chunk 语义完整性仍偏弱。"
    if report.chunk_macro_score is not None and report.chunk_macro_score < 0.5:
        return "内容大体保住了，但 chunk 组织方式还不足以支撑稳定复核。"
    return "解析和 chunk 两侧都还有优化空间，建议优先看 bad case 分布。"


def _render_counter_explanations(title: str, counter: Counter[str]) -> list[str]:
    if "解析侧" in title:
        descriptions = {
            "visual-figure-loss": "视觉图表/表格语义缺失：页面中的图、表、视觉结构信息没有被完整转成纯净文本。",
            "visual-generated-noise": "视觉生成噪声：模型补写了说明性内容，能帮助理解，但不等同于原文保留。",
            "table-loss": "表格信息缺失：表格结构存在，但关键单元格、行列语义或数据内容没有保住。",
            "metadata-loss": "元数据丢失：标题、目录、页脚、标识字段等信息未被稳定保留。",
            "glossary-loss": "术语/定义缺失：关键名词、定义项或术语解释在解析后被削弱。",
            "procedural-detail-loss": "流程细节缺失：业务流程或步骤性描述只保住了骨架，没有保住细节。",
            "other": "其他：无法稳定归入上述类型，但仍属于需要人工复核的保留问题。",
        }
    else:
        descriptions = {
            "boundary-issue": "边界截断：chunk 在章节、列表或表格边缘被硬切断，导致语义不闭合。",
            "semantic-loss": "语义缺失：chunk 内虽然有内容，但独立阅读时信息不完整。",
            "unreviewed": "未完成复核：该 chunk 在本次批量审查中未能得到稳定判定。",
            "other": "其他：不属于常见边界型问题，但仍影响 chunk 的独立可读性。",
        }

    lines = ["说明："]
    for key in counter:
        detail = descriptions.get(key)
        if detail:
            lines.append(f"- {key}：{detail}")
    return lines


def _render_optimization_suggestions() -> list[str]:
    return [
        "### 3.1 先做：边界感知 chunk 规则收紧",
        "",
        "效果：高",
        "",
        "难度：中",
        "",
        "建议：",
        "- 对标题、列表、表格、代码块、流程图说明建立更明确的切分规则。",
        "- 在章节结尾加入不要在标题、冒号、列表半截处收尾的边界校验。",
        "- 对跨章拼接的 chunk 加上显式告警，避免把上一章尾巴和下一章开头硬拼到一起。",
        "",
        "原因：当前 chunk bad case 的主因就是边界切错，而不是模型看不懂。先修边界，收益最直接。",
        "",
        "### 3.2 先做：页级/块级证据关联增强",
        "",
        "效果：高",
        "",
        "难度：中偏低",
        "",
        "建议：",
        "- 保留 page/page-like 位置映射，把图像 chunk、表格 chunk 和对应页面文本绑定得更紧。",
        "- 对 PDF 的图表页，优先把结构化内容从 chunk 证据里拉回来，而不是只看短 segment 文本。",
        "- 在审查报告里保留命中的 chunk_id / page 信息，方便回溯 bad case。",
        "",
        "原因：PDF 的高低分差异已经说明，单看 segment 文本会漏掉已经入库的完整图表证据。",
        "",
        "### 3.3 再做：视觉 judge 的输出校验和一次重试",
        "",
        "效果：中高",
        "",
        "难度：低",
        "",
        "建议：",
        "- 对视觉 judge 的返回做更严格的结构校验，避免空返回、非 JSON、异常文本直接造成整页 unreviewed。",
        "- 对 400/429/短暂网络抖动做一次有限重试，但仍保留失败记录，不做静默降级。",
        "- 在报告中明确区分视觉内容本身低质和视觉 judge 调用失败。",
        "",
        "原因：这能减少误伤，但不会掩盖真正 bad case，符合当前审查目标。",
        "",
        "### 3.4 再做：旧 chunk 兼容与 provenance 标记",
        "",
        "效果：中",
        "",
        "难度：低",
        "",
        "建议：",
        "- 对 legacy_plain_text_content、旧格式 chunk 增加更明确的 provenance 标记。",
        "- 让报告能区分原始纯文本 chunk 和视觉/生成补写 chunk。",
        "",
        "原因：现在报告里已经能看到不少 legacy chunk，继续把来源信息标清楚，可以减少人工复核成本。",
        "",
        "### 3.5 最后做：文档类型特化阈值与 bad case 路由",
        "",
        "效果：中",
        "",
        "难度：中偏低",
        "",
        "建议：",
        "- PDF 重点看图表页、术语页、目录页的保留与边界。",
        "- DOCX 重点看段落/章节边界。",
        "- Markdown 重点看元数据和章节层级是否被保住。",
        "",
        "原因：不同格式的失真模式不同，用统一阈值能跑通，但难以持续优化。",
    ]
