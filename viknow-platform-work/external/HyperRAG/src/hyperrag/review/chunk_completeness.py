"""Deterministic diagnostics for suspicious semantic chunk boundaries."""

import asyncio
import inspect
import json
import re
from collections.abc import Callable
from typing import Any

from .models import (
    ChunkBoundaryAnalysis,
    ChunkCompletenessReview,
    NormalizedChunk,
    ReviewStatus,
    RuleFinding,
    SemanticCompletenessJudgment,
)


class ChunkBoundaryAnalyzer:
    """Flag structural boundary defects before the semantic LLM review."""

    _CONTINUATION_PREFIX = re.compile(
        r"^(?:[,，。；;:：、)）\]】]|(?:and|or|but|because|therefore)\b)",
        re.IGNORECASE,
    )
    _LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", re.MULTILINE)
    _TERMINAL = re.compile(r"[。！？.!?；;：:]\s*$")

    def analyze(self, chunk: NormalizedChunk) -> ChunkBoundaryAnalysis:
        text = chunk.content.strip()
        findings: list[RuleFinding] = []

        if not text:
            findings.append(
                RuleFinding(
                    code="empty_content",
                    severity=ReviewStatus.FAIL,
                    message="Chunk content is empty.",
                )
            )
            return self._build_result(chunk.chunk_id, findings)

        if len(text) < 20:
            findings.append(
                RuleFinding(
                    code="very_short_content",
                    severity=ReviewStatus.WARN,
                    message="Chunk is too short to reliably carry standalone semantics.",
                    evidence=text,
                )
            )

        if self._CONTINUATION_PREFIX.search(text):
            findings.append(
                RuleFinding(
                    code="leading_continuation",
                    severity=ReviewStatus.FAIL,
                    message="Chunk begins like a continuation of omitted text.",
                    evidence=text[:80],
                )
            )

        if self._LIST_ITEM.search(text) and not self._TERMINAL.search(text):
            findings.append(
                RuleFinding(
                    code="open_list_boundary",
                    severity=ReviewStatus.WARN,
                    message="List-like content ends without a clear semantic boundary.",
                    evidence=text[-80:],
                )
            )

        if text.count("```") % 2:
            findings.append(
                RuleFinding(
                    code="unbalanced_code_fence",
                    severity=ReviewStatus.FAIL,
                    message="Markdown code fence is not closed within the chunk.",
                )
            )

        if self._has_unbalanced_pairs(text):
            findings.append(
                RuleFinding(
                    code="unbalanced_delimiter",
                    severity=ReviewStatus.WARN,
                    message="Chunk contains unbalanced brackets or quotation marks.",
                )
            )

        if self._is_markdown_table_continuation(text):
            findings.append(
                RuleFinding(
                    code="table_without_header",
                    severity=ReviewStatus.FAIL,
                    message="Markdown table rows appear without a header separator.",
                    evidence=text.splitlines()[0][:120],
                )
            )

        if chunk.metadata.get("edited") is True:
            findings.append(
                RuleFinding(
                    code="modal_replay_content",
                    severity=ReviewStatus.WARN,
                    message="Chunk includes replayed text added at a modal boundary.",
                )
            )

        return self._build_result(chunk.chunk_id, findings)

    @staticmethod
    def _has_unbalanced_pairs(text: str) -> bool:
        pairs = (("(", ")"), ("[", "]"), ("（", "）"), ("【", "】"))
        if any(text.count(left) != text.count(right) for left, right in pairs):
            return True
        return text.count('"') % 2 == 1 or text.count("“") != text.count("”")

    @staticmethod
    def _is_markdown_table_continuation(text: str) -> bool:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or "|" not in lines[0]:
            return False
        separator = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
        return len(lines) < 2 or separator.fullmatch(lines[1]) is None

    @staticmethod
    def _build_result(chunk_id: str, findings: list[RuleFinding]) -> ChunkBoundaryAnalysis:
        fail_count = sum(finding.severity == ReviewStatus.FAIL for finding in findings)
        warn_count = sum(finding.severity == ReviewStatus.WARN for finding in findings)
        heuristic_score = max(0.0, 1.0 - fail_count * 0.35 - warn_count * 0.15)
        if fail_count:
            status = ReviewStatus.FAIL
        elif warn_count:
            status = ReviewStatus.WARN
        else:
            status = ReviewStatus.PASS
        return ChunkBoundaryAnalysis(
            chunk_id=chunk_id,
            status=status,
            heuristic_score=heuristic_score,
            findings=findings,
        )


class ChunkSemanticReviewer:
    def __init__(
        self,
        llm_func: Callable[..., Any],
        *,
        max_concurrent: int = 8,
        fallback_on_error: bool = False,
    ):
        self.llm_func = llm_func
        self.max_concurrent = max(1, max_concurrent)
        self.fallback_on_error = bool(fallback_on_error)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._boundary_analyzer = ChunkBoundaryAnalyzer()

    async def review(self, chunk: NormalizedChunk) -> ChunkCompletenessReview:
        boundary = self._boundary_analyzer.analyze(chunk)
        try:
            async with self._semaphore:
                response = self.llm_func(self._prompt(chunk.content), temperature=0)
                if inspect.isawaitable(response):
                    response = await response
            judgment = SemanticCompletenessJudgment.model_validate(
                self._parse_json_object(str(response or ""))
            )
        except Exception as exc:
            if self.fallback_on_error:
                fallback_status = (
                    ReviewStatus.WARN
                    if boundary.status == ReviewStatus.PASS
                    else boundary.status
                )
                return ChunkCompletenessReview(
                    chunk=chunk,
                    status=fallback_status,
                    semantic_score=boundary.heuristic_score,
                    boundary_analysis=boundary,
                    error=f"judge_failed_fallback_to_boundary: {exc}",
                )
            return ChunkCompletenessReview(
                chunk=chunk,
                status=ReviewStatus.UNREVIEWED,
                boundary_analysis=boundary,
                error=str(exc),
            )

        score = (
            judgment.semantic_completeness
            + judgment.standalone_understandability
            + judgment.boundary_integrity
        ) / 3
        if score >= 0.8:
            status = ReviewStatus.PASS
        elif score >= 0.6:
            status = ReviewStatus.WARN
        else:
            status = ReviewStatus.FAIL
        return ChunkCompletenessReview(
            chunk=chunk,
            status=status,
            semantic_score=score,
            boundary_analysis=boundary,
            judgment=judgment,
        )

    @staticmethod
    def _prompt(content: str) -> str:
        return (
            "你是知识索引质量评审员。只评审下面单个 chunk，不使用或假设相邻 chunk。"
            "判断它是否独立表达完整语义，开头和结尾是否在合理语义边界。"
            "只输出 JSON 对象，字段必须为 semantic_completeness、"
            "standalone_understandability、boundary_integrity（0到1小数），"
            "missing_context_types（字符串数组）、bad_span（字符串或null）、reason（字符串）。\n"
            f"<chunk>\n{content}\n</chunk>"
        )

    @staticmethod
    def _parse_json_object(response: str) -> dict[str, Any]:
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("judge did not return a JSON object")
        payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("judge JSON response must be an object")
        return payload
