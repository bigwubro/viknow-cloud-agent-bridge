"""Text evidence inspector — single-stage LLM extraction + fuzzy-match verification.

Unlike video/image inspect which use two-stage VLM (extract + self-check),
text_inspect uses a single LLM call for batch extraction across all text
segments, followed by deterministic fuzzy-match verification using difflib.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage
from loguru import logger
from pydantic import BaseModel, Field

from agents.expert_buddy.core.evidence import Evidence, EvidencePartKind
from agents.expert_buddy.core.multimodal.structured_llm import invoke_structured_llm
from agents.expert_buddy.core.multimodal.text_inspect_claim import (
    TextEvidenceSpan,
    TextInspectClaim,
    TextInspectResult,
)

# ---------------------------------------------------------------------------
# LLM structured output model (internal — only claim/source_segments/supporting_quote/confidence)
# ---------------------------------------------------------------------------


class _TextInspectClaimItem(BaseModel):
    """Single claim item returned by LLM (before verification)."""

    claim: str
    evidence_spans: list[_TextEvidenceSpanItem] = Field(default_factory=list)
    source_segments: list[str] = Field(default_factory=list)
    supporting_quote: str = ""
    confidence: float = Field(ge=0, le=1, default=0.0)


class _TextEvidenceSpanItem(BaseModel):
    """One raw quote span returned by the LLM."""

    segment_id: str = ""
    supporting_quote: str = ""


class _TextInspectClaimsList(BaseModel):
    """Structured output model for the LLM batch extraction call."""

    claims: list[_TextInspectClaimItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# TextEvidenceInspector
# ---------------------------------------------------------------------------


class TextEvidenceInspector:
    """Extract claims from text evidence via single LLM call + fuzzy-match verification."""

    def __init__(self, *, chat_model, repo_root: Path) -> None:
        self.chat_model = chat_model
        self.repo_root = repo_root.resolve()

    def extract(self, evidence: Evidence, question: str) -> TextInspectResult:
        """Run text claim extraction and quote verification on a single text evidence."""
        query = (question or "").strip()
        if not query:
            return TextInspectResult(success=False, error="question is required")

        full_text = _first_text_part(evidence)
        if not full_text:
            return TextInspectResult(success=False, error="evidence has no TEXT part")

        locations: list[dict] = evidence.metadata.get("locations", [])
        if not locations:
            # Fallback: treat the entire text as a single segment
            locations = [{"segment_id": "seg_0001", "text": full_text}]

        # Filter noise segments
        kept = _filter_locations(locations)
        if not kept:
            return TextInspectResult(success=False, error="all text segments filtered as noise")
        logger.bind(
            evidence_id=evidence.id,
            question_chars=len(query),
            original_segments_count=len(locations),
            kept_segments_count=len(kept),
        ).info("text_inspect extraction started")

        # Build prompt and call LLM
        prompt = _build_prompt(kept, query)
        try:
            llm_started = perf_counter()
            result = invoke_structured_llm(
                self.chat_model,
                _TextInspectClaimsList,
                [HumanMessage(content=prompt)],
                run_name="text_inspect_extract",
            )
            logger.bind(
                evidence_id=evidence.id,
                llm_elapsed_seconds=round(perf_counter() - llm_started, 3),
                raw_claims_count=len(result.claims) if result is not None else 0,
            ).info("text_inspect llm extraction completed")
        except Exception as exc:
            logger.exception("text_inspect extraction failed")
            return TextInspectResult(success=False, error=f"text_inspect failed: {exc}")

        if result is None:
            logger.warning("text_inspect returned no structured output")
            return TextInspectResult(success=True, claims=[])

        segment_texts = _locations_by_segment(kept)

        # Verify each claim's supporting_quote against concrete source segments.
        verify_started = perf_counter()
        claims: list[TextInspectClaim] = []
        for item in result.claims:
            if not item.claim.strip():
                continue
            raw_spans = _claim_evidence_spans(item)
            evidence_spans = [
                _verify_evidence_span(span, segment_texts)
                for span in raw_spans
                if span.segment_id.strip() or span.supporting_quote.strip()
            ]
            verified_segments = [
                span.segment_id
                for span in evidence_spans
                if span.quote_verified and span.segment_id
            ]
            unverified_segments = [
                span.segment_id
                for span in evidence_spans
                if not span.quote_verified and span.segment_id
            ]
            fallback_segments = [span.segment_id for span in evidence_spans if span.segment_id] or [
                str(segment_id).strip()
                for segment_id in getattr(item, "source_segments", [])
                if str(segment_id).strip()
            ]
            resolved_segments = verified_segments or fallback_segments
            verified = bool(evidence_spans) and all(span.quote_verified for span in evidence_spans)
            ratio = min((span.match_ratio for span in evidence_spans), default=0.0)
            corrected_span_reasons = [
                span.verify_reason
                for span in evidence_spans
                if "corrected source segment" in span.verify_reason
            ]
            if corrected_span_reasons:
                reason = "; ".join(corrected_span_reasons)
            elif verified:
                reason = f"all evidence spans verified (min_ratio={ratio:.2f})"
            elif verified_segments:
                reason = (
                    "some evidence spans were not verified "
                    f"(verified={len(verified_segments)}, unverified={len(unverified_segments)})"
                )
            else:
                reason = f"quote not found in any source segment (best_ratio={ratio:.2f})"
            claims.append(
                TextInspectClaim(
                    claim=item.claim,
                    source_segments=resolved_segments,
                    supporting_quote=item.supporting_quote,
                    evidence_spans=evidence_spans,
                    verified_source_segments=verified_segments,
                    unverified_source_segments=unverified_segments,
                    confidence=item.confidence,
                    quote_verified=verified,
                    match_ratio=ratio,
                    verify_reason=reason,
                )
            )

        logger.bind(
            evidence_id=evidence.id,
            claims_count=len(claims),
            verify_elapsed_seconds=round(perf_counter() - verify_started, 3),
        ).info("text_inspect quote verification completed")
        return TextInspectResult(success=True, claims=claims)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _first_text_part(evidence: Evidence) -> str:
    """Return the text from the first TEXT part in evidence.parts."""
    for part in evidence.parts:
        if part.kind == EvidencePartKind.TEXT and part.text:
            return part.text
    return ""


def _is_noise_segment(text: str) -> bool:
    """Check if a text segment is noise (empty, pure symbols, or too short)."""
    stripped = text.strip()
    if not stripped:
        return True
    # Pure symbols/punctuation with no letters or digits
    has_alpha = any(c.isalpha() for c in stripped)
    has_digit = any(c.isdigit() for c in stripped)
    if not has_alpha and not has_digit:
        return True
    # Length < 3 and no Chinese characters
    if len(stripped) < 3:
        has_cjk = any("\u4e00" <= c <= "\u9fff" for c in stripped)
        if not has_cjk:
            return True
    return False


def _filter_locations(locations: list[dict]) -> list[dict]:
    """Filter out noise segments from metadata.locations."""
    kept: list[dict] = []
    for loc in locations:
        text = str(loc.get("text", "") or "")
        if not _is_noise_segment(text):
            kept.append(loc)
    return kept


def _locations_by_segment(locations: list[dict]) -> dict[str, str]:
    """Map segment IDs to their text payloads."""
    mapping: dict[str, str] = {}
    for loc in locations:
        segment_id = str(loc.get("segment_id") or "").strip()
        text = str(loc.get("text") or "")
        if segment_id and text:
            mapping[segment_id] = text
    return mapping


def _build_prompt(locations: list[dict], question: str) -> str:
    """Build the batch extraction prompt for text_inspect."""
    segments_block = ""
    for loc in locations:
        seg_id = loc.get("segment_id", "?")
        text = loc.get("text", "") or ""
        segments_block += f"[{seg_id}]\n{text}\n\n"

    prompt = (
        f"以下文本标注了段落ID。提取与问题相关的事实声明：\n\n"
        f"{segments_block}"
        f"针对问题「{question}」，提取所有相关的事实声明。\n"
        "每条必须包含：\n"
        "- claim: 事实声明（只提取原文明确陈述的内容，禁止推理或补充）\n"
        "- evidence_spans: 支撑该事实的原文片段列表；每个片段包含 segment_id 和 supporting_quote\n"
        "- source_segments: 该事实涉及的段落ID列表\n"
        "- supporting_quote: 原文原句\n"
        "- confidence: 原文陈述明确度（0-1，1=原文直接写明，0.7=明确暗示，0.5以下不应提取）\n"
        "规则：每个 evidence_spans[].supporting_quote 必须是对应 segment_id 中连续出现的原文，"
        "禁止跨 segment 拼接 quote，禁止改写原文。"
    )
    return prompt


def _claim_evidence_spans(item: object) -> list[TextEvidenceSpan]:
    spans: list[TextEvidenceSpan] = []
    for raw_span in getattr(item, "evidence_spans", []) or []:
        if isinstance(raw_span, TextEvidenceSpan):
            spans.append(raw_span)
            continue
        if isinstance(raw_span, BaseModel):
            payload = raw_span.model_dump()
        elif isinstance(raw_span, dict):
            payload = raw_span
        else:
            payload = {
                "segment_id": getattr(raw_span, "segment_id", ""),
                "supporting_quote": getattr(raw_span, "supporting_quote", ""),
            }
        spans.append(
            TextEvidenceSpan(
                segment_id=str(payload.get("segment_id") or "").strip(),
                supporting_quote=str(payload.get("supporting_quote") or ""),
            )
        )
    if spans:
        return spans

    supporting_quote = str(getattr(item, "supporting_quote", "") or "")
    return [
        TextEvidenceSpan(segment_id=str(segment_id).strip(), supporting_quote=supporting_quote)
        for segment_id in getattr(item, "source_segments", [])
        if str(segment_id).strip()
    ]


def _verify_evidence_span(
    span: TextEvidenceSpan,
    segment_texts: dict[str, str],
) -> TextEvidenceSpan:
    verified, ratio = _verify_quote(span.supporting_quote, segment_texts.get(span.segment_id, ""))
    if verified:
        return span.model_copy(
            update={
                "quote_verified": True,
                "match_ratio": ratio,
                "verify_reason": f"quote matches declared segment (ratio={ratio:.2f})",
            }
        )

    best_segment, best_ratio = _best_quote_segment(span.supporting_quote, segment_texts)
    if best_segment and best_ratio >= 0.85:
        return span.model_copy(
            update={
                "segment_id": best_segment,
                "quote_verified": True,
                "match_ratio": best_ratio,
                "verify_reason": (
                    "quote matched a different source segment; corrected source segment "
                    f"to {best_segment} (ratio={best_ratio:.2f})"
                ),
            }
        )
    return span.model_copy(
        update={
            "quote_verified": False,
            "match_ratio": best_ratio,
            "verify_reason": (
                f"quote not found in declared or fallback segments (best_ratio={best_ratio:.2f})"
            ),
        }
    )


def _verify_quote(quote: str, full_text: str) -> tuple[bool, float]:
    """Verify that a supporting quote exists in the full text using fuzzy matching.

    Uses difflib.SequenceMatcher.find_longest_match() to find the longest
    contiguous matching block, then computes the coverage ratio.

    Returns:
        (verified, match_ratio) tuple.
    """
    if not quote or not full_text:
        return False, 0.0

    normalized_quote = _normalize_quote_text(quote)
    normalized_full_text = _normalize_quote_text(full_text)
    if not normalized_quote or not normalized_full_text:
        return False, 0.0

    # Exact containment is authoritative. SequenceMatcher's default autojunk
    # heuristic can miss even verbatim spans in long, repetitive source text.
    if normalized_quote in normalized_full_text:
        return True, 1.0

    matcher = SequenceMatcher(None, normalized_quote, normalized_full_text)
    match = matcher.find_longest_match(
        0,
        len(normalized_quote),
        0,
        len(normalized_full_text),
    )

    match_ratio = match.size / len(normalized_quote)
    verified = match_ratio >= 0.85
    return verified, match_ratio


def _normalize_quote_text(value: str) -> str:
    """Fold whitespace/Markdown escapes, preserving unit case and exponents."""
    normalized = re.sub(r"\\([\\`*{}\[\]()#+\-.!_$])", r"\1", value)
    return " ".join(normalized.split())


def _verify_quote_in_segments(
    quote: str,
    source_segments: list[str],
    segment_texts: dict[str, str],
) -> tuple[bool, float, list[str]]:
    """Verify quote against declared source segments."""
    best_ratio = 0.0
    matched: list[str] = []
    for segment_id in source_segments:
        text = segment_texts.get(str(segment_id), "")
        verified, ratio = _verify_quote(quote, text)
        best_ratio = max(best_ratio, ratio)
        if verified:
            matched.append(str(segment_id))
    return bool(matched), best_ratio, matched


def _best_quote_segment(quote: str, segment_texts: dict[str, str]) -> tuple[str | None, float]:
    """Return the segment with the strongest quote match."""
    best_segment: str | None = None
    best_ratio = 0.0
    for segment_id, text in segment_texts.items():
        _verified, ratio = _verify_quote(quote, text)
        if ratio > best_ratio:
            best_segment = segment_id
            best_ratio = ratio
    return best_segment, best_ratio
