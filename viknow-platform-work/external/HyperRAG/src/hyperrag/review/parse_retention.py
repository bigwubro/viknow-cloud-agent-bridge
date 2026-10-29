"""Information-retention review for parser snapshots."""

import asyncio
import inspect
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from .models import (
    NormalizedChunk,
    ParsedEvidence,
    ParseRetentionReview,
    RetentionFinding,
    ReviewStatus,
    SourceInformationUnit,
)


def build_parsed_evidence(segments: list[dict[str, Any]]) -> list[ParsedEvidence]:
    evidence: list[ParsedEvidence] = []
    for segment in segments:
        extra = segment.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        source = str(extra.get("source") or "").lower()
        evidence.append(
            ParsedEvidence(
                segment_id=str(segment.get("segment_id") or ""),
                kind=str(segment.get("type") or "text"),
                content=str(segment.get("text") or ""),
                locator=dict(segment.get("location") or {}),
                generated="视觉要点" in str(segment.get("text") or "")
                or source in {"vlm", "image_enhancer"},
            )
        )
    return evidence


def build_chunk_evidence(chunks: list[NormalizedChunk]) -> list[ParsedEvidence]:
    """Build supplemental parsed evidence from localized chunk storage.

    This is mainly used for image/table-heavy pages where segment text may be short
    while persisted chunk content already includes richer extracted text.
    """
    evidence: list[ParsedEvidence] = []
    for chunk in chunks:
        locator = _chunk_locator(chunk)
        if not locator:
            # Keep supplementation localized to avoid global false positives.
            continue

        kind = str(chunk.metadata.get("chunk_type") or "chunk")
        content = str(chunk.content or "")
        generated = _is_generated_chunk(chunk, content)
        evidence.append(
            ParsedEvidence(
                segment_id=f"chunk:{chunk.chunk_id}",
                kind=kind,
                content=content,
                locator=locator,
                generated=generated,
            )
        )
    return evidence


def _chunk_locator(chunk: NormalizedChunk) -> dict[str, Any]:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    locations = metadata.get("locations")
    if isinstance(locations, list):
        for item in locations:
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            if isinstance(page, int):
                return {"page": page}
            slide = item.get("slide")
            if isinstance(slide, int):
                return {"slide": slide}

    page = metadata.get("page")
    if isinstance(page, int):
        return {"page": page}
    slide = metadata.get("slide")
    if isinstance(slide, int):
        return {"slide": slide}
    return {}


def _is_generated_chunk(chunk: NormalizedChunk, content: str) -> bool:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    chunk_type = str(metadata.get("chunk_type") or "").lower()
    text = unicodedata.normalize("NFKC", content)
    if "视觉要点" in text:
        return True
    if chunk_type in {"image", "vision"} and "技术报告" in text:
        return True
    return False


class DeterministicRetentionReviewer:
    def review(
        self,
        source_units: list[SourceInformationUnit],
        parsed_evidence: list[ParsedEvidence],
    ) -> ParseRetentionReview:
        findings: list[RetentionFinding] = []
        reviewed_weight = 0.0
        retained_weight = 0.0
        unreviewed_weight = 0.0
        risk_codes: set[str] = set()

        original_evidence = [item for item in parsed_evidence if not item.generated]
        for source_unit in source_units:
            risk_codes.update(source_unit.risk_codes)
            if not source_unit.reviewable:
                unreviewed_weight += source_unit.weight
                visual_failures = [
                    code
                    for code in source_unit.risk_codes
                    if isinstance(code, str) and code.startswith("visual_judge_failed:")
                ]
                reason = "Source information requires model or manual review."
                if visual_failures:
                    reason = (
                        "Visual judge failed for this source unit: "
                        + "; ".join(visual_failures[:2])
                    )
                findings.append(
                    RetentionFinding(
                        source_unit=source_unit,
                        status=ReviewStatus.UNREVIEWED,
                        score=0.0,
                        reason=reason,
                    )
                )
                continue

            reviewed_weight += source_unit.weight
            score, matched_ids = self._best_match(source_unit, original_evidence)
            retained_weight += source_unit.weight * score
            if score >= 0.85:
                status = ReviewStatus.PASS
            elif score >= 0.5:
                status = ReviewStatus.WARN
            else:
                status = ReviewStatus.FAIL
            findings.append(
                RetentionFinding(
                    source_unit=source_unit,
                    status=status,
                    score=score,
                    matched_segment_ids=matched_ids,
                    reason="Deterministic normalized-token coverage.",
                )
            )

        retention = retained_weight / reviewed_weight if reviewed_weight else None
        if retention is None:
            status = ReviewStatus.UNREVIEWED
        elif retention >= 0.85:
            status = ReviewStatus.PASS
        elif retention >= 0.7:
            status = ReviewStatus.WARN
        else:
            status = ReviewStatus.FAIL
        return ParseRetentionReview(
            status=status,
            information_retention=retention,
            reviewed_weight=reviewed_weight,
            unreviewed_weight=unreviewed_weight,
            findings=findings,
            risk_codes=sorted(risk_codes),
        )

    def _best_match(
        self,
        source_unit: SourceInformationUnit,
        evidence: list[ParsedEvidence],
    ) -> tuple[float, list[str]]:
        source_tokens = self._tokens(source_unit.content)
        if not source_tokens:
            return 0.0, []
        candidates = self._candidate_evidence(source_unit, evidence)
        best_score = 0.0
        best_ids: list[str] = []
        for candidate in candidates:
            candidate_tokens = self._tokens(candidate.content)
            score = len(source_tokens & candidate_tokens) / len(source_tokens)
            if score > best_score:
                best_score = score
                best_ids = [candidate.segment_id]
        return best_score, best_ids

    @staticmethod
    def _candidate_evidence(
        source_unit: SourceInformationUnit, evidence: list[ParsedEvidence]
    ) -> list[ParsedEvidence]:
        page = source_unit.locator.get("page")
        if page is None:
            slide = source_unit.locator.get("slide")
            page = slide - 1 if isinstance(slide, int) else None
        if page is None:
            return evidence
        localized = [item for item in evidence if item.locator.get("page") == page]
        return localized or evidence

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        return set(re.findall(r"[\u4e00-\u9fff]|[a-z0-9_]+", normalized))


class ModelAssistedRetentionReviewer:
    def __init__(
        self,
        llm_func: Callable[..., Any],
        *,
        max_concurrent: int = 8,
        deterministic_acceptance: float = 0.85,
    ):
        self.llm_func = llm_func
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self.max_concurrent = max(1, max_concurrent)
        self.deterministic_acceptance = deterministic_acceptance
        self._deterministic = DeterministicRetentionReviewer()

    async def review(
        self,
        source_units: list[SourceInformationUnit],
        parsed_evidence: list[ParsedEvidence],
    ) -> ParseRetentionReview:
        base = self._deterministic.review(source_units, parsed_evidence)
        candidates = [
            finding
            for finding in base.findings
            if finding.source_unit.reviewable and finding.score < self.deterministic_acceptance
        ]
        await asyncio.gather(
            *(self._review_finding(finding, parsed_evidence) for finding in candidates)
        )

        reviewed = [finding for finding in base.findings if finding.source_unit.reviewable]
        reviewed_weight = sum(finding.source_unit.weight for finding in reviewed)
        retained_weight = sum(finding.source_unit.weight * finding.score for finding in reviewed)
        base.information_retention = retained_weight / reviewed_weight if reviewed_weight else None
        if base.information_retention is None or any(
            finding.status == ReviewStatus.UNREVIEWED for finding in reviewed
        ):
            base.status = ReviewStatus.UNREVIEWED
        elif base.information_retention >= 0.85:
            base.status = ReviewStatus.PASS
        elif base.information_retention >= 0.7:
            base.status = ReviewStatus.WARN
        else:
            base.status = ReviewStatus.FAIL
        return base

    async def _review_finding(
        self,
        finding: RetentionFinding,
        parsed_evidence: list[ParsedEvidence],
    ) -> None:
        evidence = self._select_evidence(finding.source_unit, parsed_evidence)
        try:
            async with self._semaphore:
                response = self.llm_func(self._prompt(finding.source_unit, evidence), temperature=0)
                if inspect.isawaitable(response):
                    response = await response
            payload = self._parse_json_object(str(response or ""))
            score = float(payload["retention_score"])
            if not 0.0 <= score <= 1.0:
                raise ValueError("retention_score must be between 0 and 1")
            finding.score = score
            finding.reason = str(payload.get("reason") or "Model-assisted alignment.")
            matched = payload.get("matched_segment_ids")
            if isinstance(matched, list):
                finding.matched_segment_ids = [str(item) for item in matched]
            if score >= 0.85:
                finding.status = ReviewStatus.PASS
            elif score >= 0.5:
                finding.status = ReviewStatus.WARN
            else:
                finding.status = ReviewStatus.FAIL
        except Exception as exc:
            # Keep deterministic score/status as fallback when model review fails.
            finding.reason = (
                f"{finding.reason} | Model-assisted review failed: {exc}"
            )

    @staticmethod
    def _select_evidence(
        source_unit: SourceInformationUnit,
        parsed_evidence: list[ParsedEvidence],
    ) -> list[ParsedEvidence]:
        candidates = DeterministicRetentionReviewer._candidate_evidence(
            source_unit, parsed_evidence
        )
        return candidates[:12]

    @staticmethod
    def _prompt(source_unit: SourceInformationUnit, evidence: list[ParsedEvidence]) -> str:
        evidence_payload = [
            {
                "segment_id": item.segment_id,
                "type": item.kind,
                "content": item.content[:2000],
                "generated": item.generated,
            }
            for item in evidence
        ]
        return (
            "你是文档解析信息留存评审员。判断原始信息单元的语义在解析证据中保留了多少。"
            "允许改写和重排，但不能把生成式描述中未经原文支持的扩写算作保留。"
            "只输出 JSON 对象：retention_score（0到1）、matched_segment_ids"
            "（字符串数组）、reason。\n"
            f"原始信息：{source_unit.content}\n"
            f"解析证据：{json.dumps(evidence_payload, ensure_ascii=False)}"
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
