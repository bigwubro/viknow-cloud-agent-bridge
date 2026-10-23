"""Two-stage image evidence extractor (stage-1 extraction + stage-2 self-check).

Stage 1: VLM examines the image and extracts structured facts (claim +
source_image_id + confidence). Stage 2: the VLM re-examines the same image
and scores each fact for visual agreement, producing a confidence + reason per
fact. Results merge into :class:`ImageVisualClaim` objects returned to the
agent.

Sync-only on purpose: tool handlers run in worker threads, so we call
``invoke`` rather than ``ainvoke`` to avoid binding langchain primitives to the
uvicorn event loop (see the cross-loop issue that bit knowledge_search).
"""

from __future__ import annotations

import base64
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage
from loguru import logger

from viknow.core.evidence import Evidence, EvidencePart, EvidencePartKind
from viknow.core.multimodal.evidence_utils import first_text, resolve_media_path
from viknow.core.multimodal.image_inspect_claim import (
    ImageClaimScore,
    ImageClaimScoreResult,
    ImageReviewResult,
    ImageVisualClaim,
    ImageVisualFact,
    ImageVisualResult,
)
from viknow.core.multimodal.structured_llm import invoke_structured_llm

_MAX_IMAGES = 10
_DEFAULT_MIME = "image/jpeg"


class ImageEvidenceExtractor:
    """Extract image evidence via two-stage VLM calls (extract + self-check)."""

    def __init__(self, *, chat_model, repo_root: Path) -> None:
        self.chat_model = chat_model
        self.repo_root = repo_root.resolve()

    def extract(self, evidence: Evidence, question: str) -> ImageReviewResult:
        """Run stage-1 extraction and stage-2 self-check on image evidence."""
        query = (question or "").strip()
        if not query:
            return ImageReviewResult(success=False, error="question is required")

        image_parts = _image_parts(evidence)
        if not image_parts:
            return ImageReviewResult(success=False, error="evidence has no IMAGE part with uri")

        caption = first_text(evidence)
        all_claims: list[ImageVisualClaim] = []
        logger.bind(
            evidence_id=evidence.id,
            image_parts_count=len(image_parts),
            question_chars=len(query),
        ).info("image inspect extraction started")

        try:
            for part in image_parts[:_MAX_IMAGES]:
                if not (part.uri or "").strip():
                    continue
                image_path = resolve_media_path(self.repo_root, part.uri)
                mime_type = part.mime_type or _DEFAULT_MIME
                read_started = perf_counter()
                img_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
                image_id = Path(part.uri).name if part.uri else ""
                logger.bind(
                    evidence_id=evidence.id,
                    image_id=image_id,
                    image_bytes=image_path.stat().st_size,
                    image_base64_chars=len(img_b64),
                    image_read_elapsed_seconds=round(perf_counter() - read_started, 3),
                    mime_type=mime_type,
                ).info("image inspect image encoded")

                stage1_started = perf_counter()
                facts = self._stage1_extract(img_b64, mime_type, query, caption, image_id)
                logger.bind(
                    evidence_id=evidence.id,
                    image_id=image_id,
                    facts_count=len(facts),
                    stage_elapsed_seconds=round(perf_counter() - stage1_started, 3),
                ).info("image inspect stage-1 completed")
                if not facts:
                    continue
                stage2_started = perf_counter()
                claims = self._stage2_self_check(img_b64, mime_type, query, facts)
                logger.bind(
                    evidence_id=evidence.id,
                    image_id=image_id,
                    claims_count=len(claims),
                    stage_elapsed_seconds=round(perf_counter() - stage2_started, 3),
                ).info("image inspect stage-2 completed")
                all_claims.extend(claims)
        except Exception as exc:
            logger.exception("image review extraction failed")
            return ImageReviewResult(success=False, error=f"image review failed: {exc}")

        return ImageReviewResult(success=True, claims=all_claims)

    def _stage1_extract(
        self,
        img_b64: str,
        mime_type: str,
        query: str,
        caption: str,
        image_id: str,
    ) -> list[ImageVisualFact]:
        prompt = _stage1_prompt(query, caption, image_id)
        message = _image_message(prompt, img_b64, mime_type)
        result = invoke_structured_llm(
            self.chat_model,
            ImageVisualResult,
            [message],
            run_name="image_inspect_stage1",
        )
        if result is None:
            logger.warning("image review stage-1 returned no structured output")
            return []
        return [fact for fact in result.facts if fact.claim.strip()]

    def _stage2_self_check(
        self,
        img_b64: str,
        mime_type: str,
        query: str,
        facts: list[ImageVisualFact],
    ) -> list[ImageVisualClaim]:
        prompt = _stage2_prompt(query, facts)
        message = _image_message(prompt, img_b64, mime_type)
        result = invoke_structured_llm(
            self.chat_model,
            ImageClaimScoreResult,
            [message],
            run_name="image_inspect_stage2",
        )

        scores_by_index: dict[int, ImageClaimScore] = {}
        stage2_ok = result is not None
        if stage2_ok:
            for score in result.scores:
                scores_by_index[score.fact_index] = score
        else:
            logger.warning(
                "image review stage-2 returned no structured output; using stage-1 confidence"
            )

        claims: list[ImageVisualClaim] = []
        for index, fact in enumerate(facts):
            score = scores_by_index.get(index + 1)
            confidence = fact.confidence
            reason = ""
            if score is not None:
                confidence = score.confidence
                reason = score.reason
            elif not stage2_ok:
                reason = "self-check unavailable"
            claims.append(
                ImageVisualClaim(
                    claim=fact.claim,
                    source_image_id=fact.source_image_id,
                    confidence=confidence,
                    verify_reason=reason,
                )
            )
        return claims


def _image_parts(evidence: Evidence) -> list[EvidencePart]:
    return [part for part in evidence.parts if part.kind == EvidencePartKind.IMAGE]


def _image_message(prompt: str, img_b64: str, mime_type: str) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{img_b64}"},
            },
        ]
    )


def _stage1_prompt(query: str, caption: str, image_id: str) -> str:
    lines = [
        f"针对问题「{query}」，仔细查看图片，提取能够回答该问题的视觉证据。",
        "",
        "要求：",
        "- 以图片画面为准，只描述你确实观察到的内容；证据不足时宁可少给。",
        "- 每条证据需给出：claim（客观陈述）、source_image_id（图片标识）、confidence（0-1）。",
        "- 优先提取与问题直接相关的事实，避免无关描述。",
    ]
    if image_id:
        lines.append(f"- 图片标识：{image_id}")
    if caption:
        lines.append(f"- 补充上下文（caption，仅供参考，以图片画面为准）：{caption}")
    return "\n".join(lines)


def _stage2_prompt(query: str, facts: list[ImageVisualFact]) -> str:
    facts_block = "\n".join(f"{i + 1}. {fact.claim}" for i, fact in enumerate(facts))
    return "\n".join(
        [
            f"针对问题「{query}」，以下是从此图片中提取的 {len(facts)} 条证据：",
            "",
            facts_block,
            "",
            "请重新查看图片，逐条核对这些证据：",
            "1. 判断每条证据与图片内容是否相符；",
            f"2. 为每条证据输出 fact_index（对应上面的序号 1-{len(facts)}）、"
            "confidence（0-1，1=完全相符，0=完全不符）、reason（打分理由）。",
        ]
    )
