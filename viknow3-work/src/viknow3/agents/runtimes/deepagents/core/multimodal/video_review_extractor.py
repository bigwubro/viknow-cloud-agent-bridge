"""Two-stage video evidence extractor (stage-1 extraction + stage-2 self-check).

Stage 1: VLM watches the submitted clip and extracts structured facts with
clip-relative timestamps. Stage 2: the VLM re-watches the same clip and scores
each fact for visual/timestamp agreement, still using clip-relative
timestamps. The extractor then converts them into original-video absolute
seconds before returning :class:`VideoVisualClaim` objects.

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

from viknow3.agents.runtimes.deepagents.core.evidence import Evidence, EvidencePart, EvidencePartKind
from viknow3.agents.runtimes.deepagents.core.multimodal.evidence_utils import first_text, resolve_media_path
from viknow3.agents.runtimes.deepagents.core.multimodal.review import review_video_path
from viknow3.agents.runtimes.deepagents.core.multimodal.structured_llm import (
    collect_structured_llm_traces,
    invoke_structured_llm,
    structured_llm_trace_token_usage,
)
from viknow3.agents.runtimes.deepagents.core.multimodal.video_review_claim import (
    ClaimScore,
    ClaimScoreResult,
    VideoReviewResult,
    VideoVisualClaim,
    VideoVisualFact,
    VideoVisualResult,
)
from viknow3.agents.runtimes.deepagents.inspect_trace import record_inspect_stage

MIN_VERIFIED_CLAIM_CONFIDENCE = 0.5
NEGATIVE_ABSENCE_CONFIDENCE_CAP = 0.4
NEGATIVE_ABSENCE_MARKERS = (
    "未观察到",
    "未出现",
    "没有观察到",
    "没有看到",
    "未看到",
    "没有发生",
    "保持静止",
)


class VideoEvidenceExtractor:
    """Extract video evidence via two-stage VLM calls (extract + self-check)."""

    def __init__(self, *, chat_model, repo_root: Path) -> None:
        self.chat_model = chat_model
        self.repo_root = repo_root.resolve()

    def extract(self, evidence: Evidence, question: str) -> VideoReviewResult:
        """Run stage-1 extraction and stage-2 self-check on one video evidence."""
        query = (question or "").strip()
        if not query:
            return VideoReviewResult(success=False, error="question is required")

        video_part = _first_video_part(evidence)
        if video_part is None or not (video_part.uri or "").strip():
            return VideoReviewResult(success=False, error="evidence has no VIDEO part with uri")

        video_path = resolve_media_path(self.repo_root, video_part.uri)
        time_range = None
        if video_part.time_range is not None:
            time_range = (
                video_part.time_range.start_seconds,
                video_part.time_range.end_seconds,
            )
        caption = first_text(evidence)
        logger.bind(
            evidence_id=evidence.id,
            question_chars=len(query),
            has_time_range=time_range is not None,
        ).info("video inspect extraction started")

        try:
            clip_started = perf_counter()
            with review_video_path(video_path, time_range) as clip_path:
                clip_bytes = clip_path.stat().st_size
                logger.bind(
                    evidence_id=evidence.id,
                    clip_bytes=clip_bytes,
                    clip_elapsed_seconds=round(perf_counter() - clip_started, 3),
                    has_time_range=time_range is not None,
                ).info("video inspect clip prepared")
                read_started = perf_counter()
                video_b64 = base64.b64encode(clip_path.read_bytes()).decode("ascii")
                logger.bind(
                    evidence_id=evidence.id,
                    video_base64_chars=len(video_b64),
                    video_read_elapsed_seconds=round(perf_counter() - read_started, 3),
                ).info("video inspect clip encoded")
                stage1_started = perf_counter()
                with collect_structured_llm_traces() as stage1_traces:
                    facts = self._stage1_extract(video_b64, query, caption, time_range)
                stage1_elapsed = round(perf_counter() - stage1_started, 3)
                logger.bind(
                    evidence_id=evidence.id,
                    facts_count=len(facts),
                    stage_elapsed_seconds=stage1_elapsed,
                ).info("video inspect stage-1 completed")
                record_inspect_stage(
                    tool="video_inspect",
                    stage="stage1",
                    payload={
                        "evidence_id": evidence.id,
                        "facts": _dump_models(facts),
                        "elapsed_seconds": stage1_elapsed,
                        "token_usage": structured_llm_trace_token_usage(stage1_traces),
                    },
                )
                if not facts:
                    record_inspect_stage(
                        tool="video_inspect",
                        stage="final",
                        payload={"evidence_id": evidence.id, "claims": []},
                    )
                    return VideoReviewResult(success=True, claims=[])
                stage2_started = perf_counter()
                with collect_structured_llm_traces() as stage2_traces:
                    claims = self._stage2_self_check(video_b64, query, facts, time_range)
                stage2_elapsed = round(perf_counter() - stage2_started, 3)
                logger.bind(
                    evidence_id=evidence.id,
                    claims_count=len(claims),
                    stage_elapsed_seconds=stage2_elapsed,
                ).info("video inspect stage-2 completed")
                record_inspect_stage(
                    tool="video_inspect",
                    stage="stage2",
                    payload={
                        "evidence_id": evidence.id,
                        "scores": _dump_models(self._last_stage2_scores),
                        "elapsed_seconds": stage2_elapsed,
                        "token_usage": structured_llm_trace_token_usage(stage2_traces),
                    },
                )
                record_inspect_stage(
                    tool="video_inspect",
                    stage="final",
                    payload={
                        "evidence_id": evidence.id,
                        "claims": _dump_models(claims),
                    },
                )
        except Exception as exc:
            logger.exception("video review extraction failed")
            return VideoReviewResult(success=False, error=f"video review failed: {exc}")

        return VideoReviewResult(success=True, claims=claims)

    def _stage1_extract(
        self,
        video_b64: str,
        query: str,
        caption: str,
        time_range: tuple[float, float] | None,
        video_options: dict[str, object] | None = None,
    ) -> list[VideoVisualFact]:
        prompt = _stage1_prompt(query, caption, time_range)
        message = _video_message(prompt, video_b64, video_options=video_options)
        result = invoke_structured_llm(
            self.chat_model,
            VideoVisualResult,
            [message],
            run_name="video_review_stage1",
        )
        if result is None:
            logger.warning("video review stage-1 returned no structured output")
            return []
        return [_normalize_stage1_fact(fact) for fact in result.facts if fact.claim.strip()]

    def _stage2_self_check(
        self,
        video_b64: str,
        query: str,
        facts: list[VideoVisualFact],
        time_range: tuple[float, float] | None,
    ) -> list[VideoVisualClaim]:
        prompt = _stage2_prompt(query, facts)
        message = _video_message(prompt, video_b64)
        result = invoke_structured_llm(
            self.chat_model,
            ClaimScoreResult,
            [message],
            run_name="video_review_stage2",
        )

        scores_by_index: dict[int, ClaimScore] = {}
        stage2_ok = result is not None
        self._last_stage2_scores = []
        if stage2_ok:
            self._last_stage2_scores = list(result.scores)
            for score in result.scores:
                scores_by_index[score.fact_index] = score
        else:
            logger.warning(
                "video review stage-2 returned no structured output; using stage-1 confidence"
            )

        claims: list[VideoVisualClaim] = []
        filtered_count = 0
        for index, fact in enumerate(facts):
            score = scores_by_index.get(index + 1)
            claim_text = fact.claim
            ts_start = fact.timestamp_start
            ts_end = fact.timestamp_end
            confidence = fact.confidence
            reason = ""
            if score is not None:
                confidence = score.confidence
                reason = score.reason
                if score.action == "drop":
                    filtered_count += 1
                    continue
                if score.action == "revise" and score.corrected_claim:
                    claim_text = score.corrected_claim.strip()
                if score.corrected_timestamp_start is not None:
                    ts_start = score.corrected_timestamp_start
                if score.corrected_timestamp_end is not None:
                    ts_end = score.corrected_timestamp_end
            elif not stage2_ok:
                reason = "self-check unavailable"
            ts_start, ts_end = _normalize_video_timestamps(
                ts_start,
                ts_end,
                time_range=time_range,
            )
            if confidence is None:
                filtered_count += 1
                continue
            if stage2_ok and confidence < MIN_VERIFIED_CLAIM_CONFIDENCE:
                filtered_count += 1
                continue
            if not claim_text:
                filtered_count += 1
                continue
            claims.append(
                VideoVisualClaim(
                    claim=claim_text,
                    timestamp_start=ts_start,
                    timestamp_end=ts_end,
                    entity_description=fact.entity_description,
                    confidence=confidence,
                    time_uncertain=fact.time_uncertain,
                    verify_reason=reason,
                )
            )
        if filtered_count:
            logger.bind(filtered_count=filtered_count).info(
                "video inspect filtered low-confidence claims"
            )
        return claims


def _dump_models(items: list) -> list[dict]:
    dumped: list[dict] = []
    for item in items:
        if hasattr(item, "model_dump"):
            dumped.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            dumped.append(dict(item))
        else:
            dumped.append({"value": str(item)})
    return dumped


def _normalize_stage1_fact(fact: VideoVisualFact) -> VideoVisualFact:
    if fact.confidence is None or not _is_negative_absence_claim(fact.claim):
        return fact
    confidence = min(fact.confidence, NEGATIVE_ABSENCE_CONFIDENCE_CAP)
    if confidence == fact.confidence and fact.time_uncertain:
        return fact
    return fact.model_copy(
        update={
            "confidence": confidence,
            "time_uncertain": True,
        }
    )


def _is_negative_absence_claim(claim: str) -> bool:
    return any(marker in claim for marker in NEGATIVE_ABSENCE_MARKERS)


def _first_video_part(evidence: Evidence) -> EvidencePart | None:
    for part in evidence.parts:
        if part.kind == EvidencePartKind.VIDEO:
            return part
    return None


def _normalize_video_timestamps(
    timestamp_start: float,
    timestamp_end: float,
    *,
    time_range: tuple[float, float] | None,
) -> tuple[float, float]:
    """Convert clip-relative timestamps into original-video absolute seconds."""

    if time_range is None:
        return _ordered_timestamps(timestamp_start, timestamp_end)
    clip_start, clip_end = time_range
    clip_duration = max(0.0, clip_end - clip_start)
    relative_start = min(max(timestamp_start, 0.0), clip_duration)
    relative_end = min(max(timestamp_end, 0.0), clip_duration)
    relative_start, relative_end = _ordered_timestamps(relative_start, relative_end)
    return clip_start + relative_start, clip_start + relative_end


def _ordered_timestamps(timestamp_start: float, timestamp_end: float) -> tuple[float, float]:
    if timestamp_end < timestamp_start:
        return timestamp_end, timestamp_start
    return timestamp_start, timestamp_end


def _video_message(
    prompt: str,
    video_b64: str,
    *,
    video_options: dict[str, object] | None = None,
) -> HumanMessage:
    payload: dict[str, object] = {"url": f"data:video/mp4;base64,{video_b64}"}
    if video_options:
        payload.update(video_options)
    return HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "video_url",
                "video_url": payload,
            },
        ]
    )


def _stage1_prompt(query: str, caption: str, time_range: tuple[float, float] | None) -> str:
    lines = [
        f"针对问题「{query}」，仔细观看视频，提取能够回答该问题的视觉证据。",
        "",
        "要求：",
        "- 以视频画面为准，只描述你确实观察到的内容；证据不足时宁可少给。",
        "- 按问题需要输出直接回答问题的核心视觉事实；如果没有足够画面证据，可以输出空列表。",
        "- 如果问题要求计数、列举步骤、多个对象或多个异常点，可以输出多条 claim。",
        "- 每条 claim 应表达一个完整、可引用的关键事实，不要按固定时间片机械拆分，"
        "不要重复表达同一个动作、状态或结论。",
        "- 先判断问题主要要求什么，再按以下优先级提取证据：",
        "  1. 如果问题询问动作、操作、使用、放置、调整、固定、按压、打开或关闭，"
        "优先描述目标对象和目标动作本身；只要画面中可见目标对象被接触、移动、"
        "放置、调整或固定，就描述可见动作和不确定部分，不要用“未观察到”替代动作描述。",
        "  2. 如果问题询问方向、状态转换、再次发生、从某状态到另一状态，"
        "只有清楚看到前后状态或运动方向，才判断转换方向；否则描述可见动作，"
        "并明确无法确认方向或状态转换。",
        "  3. 如果问题询问计数、列举多个对象、多个步骤或多个异常点，"
        "每条 claim 对应一个独立对象、步骤或异常，不要重复同一事实。",
        "  4. 如果问题询问不存在、未出现、未观察到、没有发生，"
        "只有目标区域和关键参照物在完整片段中持续清楚可见，才输出高置信否定；"
        "否则说明无法确认，低置信。",
        "- 冲突处理：如果一个问题同时包含动作和否定判断，优先按动作处理；"
        "如果看到目标动作发生，不要输出“未观察到该动作”；"
        "如果只看到动作但看不清方向，输出动作发生并说明方向无法确认。",
        "- 如果问题询问是否发生、是否移动、动作方向、按钮从某状态转到另一状态等方向性动作，"
        "只有在画面清楚展示前后状态或运动方向时才下确定结论；无法从画面确认时，"
        "claim 必须明确说明无法确认，confidence 低于 0.5，并设置 time_uncertain=true。",
        "- 对“未出现、未观察到、没有发生、保持静止”等否定或不存在类结论，"
        "只有在完整片段中目标对象和关键参照物持续清楚可见时才可高置信输出；"
        "若存在遮挡、运动模糊、视角切换或目标离开画面，claim 必须说明证据不足，"
        "confidence 低于 0.5，并设置 time_uncertain=true。",
        "- 每条证据需给出：claim（客观陈述）、timestamp_start 与 timestamp_end"
        "（该证据出现在当前送审片段中的相对秒数，片段开头记为 0 秒）、"
        "entity_description（涉及的对象或设备）、confidence、time_uncertain"
        "（时间定位或动作方向不确定时置为 true）。",
        "- 优先提取与问题直接相关的事实，避免背景、流程铺垫或无关描述。",
    ]
    if time_range is not None:
        lines.append(
            f"- 当前送审片段对应原始视频的 {time_range[0]:.1f}s 到 {time_range[1]:.1f}s，"
            "但你输出的时间戳仍然必须是当前送审片段内的相对时间，不要自行换算成原视频绝对时间。"
        )
    if caption:
        lines.append(f"- 补充上下文（caption，仅供参考，以画面为准）：{caption}")
    return "\n".join(lines)


def _stage2_prompt(query: str, facts: list[VideoVisualFact]) -> str:
    facts_block = "\n".join(
        f"{i + 1}. {fact.claim} [{fact.timestamp_start:.1f}s - {fact.timestamp_end:.1f}s]"
        for i, fact in enumerate(facts)
    )
    return "\n".join(
        [
            f"针对问题「{query}」，以下是从此视频中提取的 {len(facts)} 条证据：",
            "",
            facts_block,
            "",
            "请重新观看视频，逐条核对这些证据：",
            "1. 判断每条证据与视频画面是否相符，尤其 timestamp_start-end 区间是否准确对应；"
            "这些时间仍是当前送审片段内的相对时间；",
            f"2. 为每条证据输出 fact_index（对应上面的序号 1-{len(facts)}）、"
            "action（只能是 keep、revise、drop）、confidence（0-1，1=完全相符，0=完全不符）、"
            "reason（打分理由）；",
            "confidence 评价 action 后的结果：keep 时评价原 claim，revise 时评价 corrected_claim，"
            "drop 时评价原 claim 不应保留的确定性。不要因为原 claim 的方向性结论不确定，"
            "就给已经修正为可见动作的 corrected_claim 低置信度；",
            "3. action=keep 表示 claim 文本和时间都准确；action=revise 表示核心视觉事实成立，"
            "但原 claim 的表达、方向性、确定性或时间段需要修正；action=drop 表示不应进入最终结果；",
            "4. revise 时可以输出 corrected_claim。只允许基于画面修正原 claim，"
            "不得补充视频中没有直接看到的新事实；如果方向或状态转换无法确认，"
            "应把 corrected_claim 改成可见动作加“无法确认方向/状态转换”的表述；"
            "不要在 corrected_claim 中写时间，时间只能通过 corrected_timestamp_start/end 输出；",
            "5. 不能因为 stage1 已提取就默认保留。若证据与问题无关、时间段不符、"
            "或核心动作无法从画面确认，confidence 必须低于 0.5，action 应为 drop，"
            "reason 明确说明无法从画面确认或不相符的原因；若只是方向性动作缺少清晰前后状态，"
            "但可见核心动作成立，应 action=revise，用 corrected_claim 表达可见动作和"
            "无法确认的方向，并按 corrected_claim 与画面的相符程度给 confidence；",
            "6. 对“未出现、未观察到、没有发生、保持静止”等否定或不存在类结论，"
            "必须确认完整片段中目标对象和关键参照物持续清楚可见；若存在遮挡、运动模糊、"
            "视角切换或目标离开画面，confidence 必须低于 0.5；",
            "7. 若时间段明显偏差且可以从画面可靠定位，给出 corrected_timestamp_start 与 "
            "corrected_timestamp_end；若时间段错误且无法可靠定位，action 应为 drop。",
        ]
    )
