"""Render deterministic JSON and Markdown index quality reports."""

import json
from pathlib import Path

from .models import IndexQualityReport, ReviewStatus


def write_report(report: IndexQualityReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: IndexQualityReport, *, bad_case_limit: int = 20) -> str:
    retention = _format_score(report.parse_review.information_retention)
    chunk_score = _format_score(report.chunk_macro_score)
    lines = [
        f"# Index Quality Review: {report.asset_id}",
        "",
        f"- Status: **{report.status.value}**",
        f"- Workspace: `{report.workspace}`",
        f"- Source: `{report.source_path}`",
        f"- Information retention: **{retention}**",
        f"- Chunk semantic completeness: **{chunk_score}**",
        f"- Unreviewed source weight: `{report.parse_review.unreviewed_weight:.2f}`",
        "",
        "## Parse Bad Cases",
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
                f"### {finding.status.value.upper()} {finding.source_unit.unit_id}",
                "",
                f"- Type: `{finding.source_unit.kind}`",
                f"- Score: `{finding.score:.3f}`",
                f"- Location: `{json.dumps(finding.source_unit.locator, ensure_ascii=False)}`",
                f"- Reason: {finding.reason}",
                f"- Source excerpt: {excerpt}",
                "",
            ]
        )

    lines.extend(["## Chunk Bad Cases", ""])
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
                f"### {review.status.value.upper()} {review.chunk.chunk_id}",
                "",
                f"- Order: `{review.chunk.order}`",
                f"- Score: `{_format_score(review.semantic_score)}`",
                f"- Rule flags: `{rule_flags}`",
                f"- Reason: {reason}",
                f"- Content excerpt: {_single_line(review.chunk.content, 260)}",
                "",
            ]
        )

    if report.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_score(score: float | None) -> str:
    return "unreviewed" if score is None else f"{score:.3f}"


def _single_line(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."
