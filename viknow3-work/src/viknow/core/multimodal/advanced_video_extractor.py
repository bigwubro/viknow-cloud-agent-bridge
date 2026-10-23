"""Adaptive information extraction from one selected video clip."""

from __future__ import annotations

import base64
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage
from loguru import logger

from viknow.core.evidence import Evidence, EvidencePart, EvidencePartKind
from viknow.core.multimodal.advanced_video_claim import (
    AdvancedClaimSupport,
    AdvancedClipStatus,
    AdvancedVideoClaim,
    AdvancedVideoClipInput,
    AdvancedVideoOptions,
    AdvancedVideoPlan,
    AdvancedVideoReviewResult,
    NormalizedCrop,
    RefinedObservation,
    RefinementRequest,
)
from viknow.core.multimodal.advanced_video_grounding import (
    GroundedCropResult,
    VideoGroundingClient,
    VideoGroundingCropper,
)
from viknow.core.multimodal.advanced_video_media import (
    extract_frame,
    fps_timestamps,
    motion_score,
    padded_crop,
    refined_video_clip,
    uniform_timestamps,
)
from viknow.core.multimodal.evidence_utils import first_text, resolve_media_path
from viknow.core.multimodal.structured_llm import (
    collect_structured_llm_traces,
    invoke_structured_llm,
    structured_llm_trace_token_usage,
)
from viknow.core.multimodal.video_review_extractor import VideoEvidenceExtractor
from viknow.core.tools.context import remaining_run_seconds
from viknow.inspect_trace import record_inspect_stage

_TOOL_NAME = "video_inspect_2_0"
_BUDGETS = {"low": (12, 12), "balanced": (24, 36), "high": (48, 64)}
_FAST_MOTION_TERMS = (
    "快速",
    "瞬间",
    "顺序",
    "动作",
    "按下",
    "打开",
    "关闭",
    "变化",
    "fast",
    "moment",
    "sequence",
    "motion",
)
_HIGH_FPS_TERMS = (
    "闪烁",
    "亮起",
    "熄灭",
    "指示灯",
    "led",
    "顺时针",
    "逆时针",
    "方向",
    "角度",
    "clockwise",
    "counter-clockwise",
    "counterclockwise",
    "blink",
    "flash",
)
_INDICATOR_EVENT_TERMS = (
    "闪烁",
    "亮起",
    "熄灭",
    "指示灯",
    "led",
    "blink",
    "flash",
)
_STATE_TRANSITION_TERMS = (
    "变化",
    "发生了变化",
    "更换",
    "换了",
    "变为",
    "变成",
    "颜色",
    "前后",
    "change",
    "changed",
    "switch",
    "replace",
    "replaced",
    "color",
    "colour",
)
_TEXT_TOOL_PRESENCE_TERMS = (
    "百洁布",
    "scotch-brite",
    "scotch brite",
    "sop",
    "标准操作程序",
    "文字",
    "标签",
    "工具",
    "清洁工具",
    "text",
    "label",
    "tool",
    "cleaning tool",
)
_HAND_CONTROL_PANEL_TERMS = (
    "控制箱",
    "控制柜",
    "控制面板",
    "电气控制",
    "按钮",
    "旋钮",
    "开关",
    "面板",
    "手部",
    "手指",
    "手套",
    "按压",
    "按下",
    "拨动",
    "转动",
    "操作",
    "control box",
    "control cabinet",
    "control panel",
    "electrical control",
    "button",
    "knob",
    "switch",
    "panel",
    "hand",
    "finger",
    "glove",
    "press",
    "push",
    "turn",
    "operate",
    "operation",
)
_SWITCH_FINAL_STATE_TERMS = (
    "急停",
    "正常位置",
    "正常",
    "复位",
    "退出",
    "最终",
    "转到",
    "切换",
    "弹起",
    "按下",
    "旋转",
    "拉出",
    "emergency stop",
    "normal position",
    "normal",
    "reset",
    "exit",
    "final",
    "switch to",
    "release",
    "press",
    "rotate",
    "pull",
)
_NEGATIVE_UNCERTAIN_TERMS = (
    "未观察到",
    "未看到",
    "无法确认",
    "无法从画面确认",
    "无法从画面中确认",
    "无法清晰判断",
    "无法准确判断",
    "无法判断",
    "不能确认",
    "没有看到",
    "没有捕捉到",
    "不清楚",
    "看不清",
    "not observe",
    "not visible",
    "not contain",
    "does not contain",
    "not support",
    "not supported",
    "no information",
    "no option",
    "cannot confirm",
    "unclear",
)
_POV_UNCERTAIN_TERMS = (
    "第一人称",
    "主观视角",
    "仅出现手",
    "镜头视角",
    "视角限制",
    "主要拍摄手部",
    "主要拍摄手",
    "仅清晰可见一只",
    "无法从画面中确认",
    "无法观察到工作人员",
    "无法观察到身体",
    "无法观察到面部",
    "pov",
    "first-person",
)
_FINE_ATTRIBUTE_TERMS = (
    "面向",
    "背对",
    "身体朝向",
    "姿势",
    "双手",
    "交替",
    "顺时针",
    "逆时针",
    "方向",
    "角度",
    "松闸扳手",
    "棘轮",
    "制动器释放",
    "不锈钢",
    "材质",
    "直径",
    "φ",
    "Φ",
    "安装在",
    "固定在",
    "attached",
    "mounted",
    "clockwise",
    "counter-clockwise",
    "counterclockwise",
    "direction",
    "angle",
)
_FACE_EXPRESSION_TERMS = (
    "表情",
    "情绪",
    "笑",
    "哭",
    "愤怒",
    "生气",
    "麻木",
    "面部",
    "神情",
    "expression",
    "emotion",
    "laugh",
    "smile",
    "cry",
    "angry",
    "numb",
    "face",
    "facial",
)
_REASONING_EVIDENCE_TERMS = (
    "推理",
    "推断",
    "证据",
    "依据",
    "infer",
    "inference",
    "evidence",
    "indicate",
    "suggest",
    "can be used",
    "two days",
    "filmed on two days",
)
_MULTIPLE_CHOICE_RE = re.compile(r"(?:^|\n)\s*\([A-H]\)\s+", re.IGNORECASE)


@dataclass
class _PreparedClip:
    evidence: Evidence
    options: AdvancedVideoClipInput
    video_part: EvidencePart
    video_path: Path
    start: float
    end: float
    caption: str
    coarse_frames: list[Path]
    coarse_timestamps: list[float]
    motion: float = 0.0


class AdvancedVideoEvidenceExtractor(VideoEvidenceExtractor):
    """Extract cited observations from one short video evidence range."""

    def __init__(
        self,
        *,
        chat_model,
        repo_root: Path,
        grounding_client: VideoGroundingClient | None = None,
    ) -> None:
        super().__init__(chat_model=chat_model, repo_root=repo_root)
        self.grounding_cropper = (
            VideoGroundingCropper(grounding_client) if grounding_client is not None else None
        )

    def extract_video(
        self,
        evidence: Evidence,
        question: str,
        *,
        clip_input: AdvancedVideoClipInput,
        options: AdvancedVideoOptions,
    ) -> AdvancedVideoReviewResult:
        remaining_run_seconds()
        query = (question or "").strip()
        if not query:
            return AdvancedVideoReviewResult(success=False, error="question is required")
        total_frames, refine_frame_limit = _BUDGETS[options.visual_budget]
        status = _initial_status(evidence, clip_input)
        started = perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="viknow-advanced-video-") as temp_value:
                temp_dir = Path(temp_value)
                coarse_max_edge = _adaptive_coarse_max_edge(query, clip_input.crop)
                status.coarse_max_edge = coarse_max_edge
                prepared = self._prepare_coarse_video(
                    evidence,
                    clip_input,
                    status,
                    temp_dir=temp_dir,
                    total_frames=total_frames,
                    max_edge=coarse_max_edge,
                )
                if prepared is None:
                    return AdvancedVideoReviewResult(
                        success=False,
                        error="video failed during coarse sampling",
                        video=status,
                    )
                with collect_structured_llm_traces() as coarse_traces:
                    plan = self._coarse_plan(prepared, query, options.instruction)
                missing_coarse_confidence_count = sum(
                    item.confidence is None for item in plan.observations
                )
                observations = _materialize_coarse_observations(plan, prepared)
                refinement = _validated_refinement(plan, prepared)
                record_inspect_stage(
                    tool=_TOOL_NAME,
                    stage="coarse",
                    payload={
                        "question": query,
                        "observations": [item.model_dump(mode="json") for item in observations],
                        "confidence_missing_count": missing_coarse_confidence_count,
                        "video": _coarse_trace_payload(prepared),
                        "refinement": refinement.model_dump(mode="json") if refinement else None,
                        "token_usage": structured_llm_trace_token_usage(coarse_traces),
                    },
                )
                refined_observations = (
                    self._refine(
                        prepared,
                        refinement,
                        status,
                        query=query,
                        instruction=options.instruction,
                        temp_dir=temp_dir,
                        frame_limit=refine_frame_limit,
                    )
                    if refinement
                    else []
                )
                observations.extend(refined_observations)
                if not observations:
                    fallback_refinement = _fallback_full_frame_refinement(
                        prepared,
                        query=query,
                        existing=refinement,
                    )
                    if fallback_refinement:
                        record_inspect_stage(
                            tool=_TOOL_NAME,
                            stage="fallback",
                            payload={
                                "reason": "no claims after planned refinement",
                                "refinement": fallback_refinement.model_dump(mode="json"),
                            },
                        )
                        fallback_observations = self._refine(
                            prepared,
                            fallback_refinement,
                            status,
                            query=query,
                            instruction=options.instruction,
                            temp_dir=temp_dir,
                            frame_limit=refine_frame_limit,
                        )
                        observations.extend(fallback_observations)
                if not observations:
                    record_inspect_stage(
                        tool=_TOOL_NAME,
                        stage="final",
                        payload={
                            "claims": [],
                            "elapsed_seconds": perf_counter() - started,
                        },
                    )
                    return AdvancedVideoReviewResult(
                        success=True,
                        claims=[],
                        video=status,
                    )
                claims = _claims_from_observations(observations)
                record_inspect_stage(
                    tool=_TOOL_NAME,
                    stage="final",
                    payload={
                        "claims": [item.model_dump(mode="json") for item in claims],
                        "video": status.model_dump(mode="json"),
                        "elapsed_seconds": round(perf_counter() - started, 3),
                    },
                )
                return AdvancedVideoReviewResult(success=True, claims=claims, video=status)
        except Exception as exc:
            logger.exception("advanced video inspection failed")
            record_inspect_stage(
                tool=_TOOL_NAME,
                stage="final",
                payload={"error": str(exc), "elapsed_seconds": round(perf_counter() - started, 3)},
            )
            return AdvancedVideoReviewResult(
                success=False,
                error=f"advanced video inspection failed: {exc}",
                video=status,
            )

    def _prepare_coarse_video(
        self,
        evidence: Evidence,
        clip_input: AdvancedVideoClipInput,
        status: AdvancedClipStatus,
        *,
        temp_dir: Path,
        total_frames: int,
        max_edge: int,
    ) -> _PreparedClip | None:
        try:
            video_part = _required_video_part(evidence)
            start, end = _video_range(evidence)
            video_path = resolve_media_path(self.repo_root, video_part.uri or "")
            timestamps = uniform_timestamps(start, end, total_frames)
            frame_paths = []
            for frame_index, timestamp in enumerate(timestamps):
                remaining_run_seconds()
                frame_path = temp_dir / f"coarse-{frame_index:03d}.jpg"
                extract_frame(
                    video_path,
                    frame_path,
                    timestamp=timestamp,
                    crop=clip_input.crop,
                    max_edge=max_edge,
                )
                frame_paths.append(frame_path)
            prepared = _PreparedClip(
                evidence=evidence,
                options=clip_input,
                video_part=video_part,
                video_path=video_path,
                start=start,
                end=end,
                caption=first_text(evidence),
                coarse_frames=frame_paths,
                coarse_timestamps=timestamps,
                motion=motion_score(frame_paths),
            )
            status.sampled_timestamps.extend(timestamps)
            status.submitted_bytes += sum(path.stat().st_size for path in frame_paths)
            status.status = "completed"
            return prepared
        except Exception as exc:
            status.status = "failed"
            status.error = str(exc)
            logger.warning("advanced coarse sampling failed for {}: {}", clip_input.clip_id, exc)
            return None

    def _coarse_plan(
        self,
        clip: _PreparedClip,
        query: str,
        instruction: str | None,
    ) -> AdvancedVideoPlan:
        content: list[dict] = [{"type": "text", "text": _coarse_prompt(clip, query, instruction)}]
        for frame_index, (timestamp, frame_path) in enumerate(
            zip(clip.coarse_timestamps, clip.coarse_frames, strict=True), start=1
        ):
            content.append(
                {
                    "type": "text",
                    "text": f"[coarse-{frame_index:03d} @ {timestamp:.3f}s absolute]",
                }
            )
            content.append(_image_content(frame_path))
        result = invoke_structured_llm(
            self.chat_model,
            AdvancedVideoPlan,
            [HumanMessage(content=content)],
            run_name="video_inspect_2_0_coarse",
        )
        return result or AdvancedVideoPlan()

    def _refine(
        self,
        clip: _PreparedClip,
        request: RefinementRequest,
        status: AdvancedClipStatus,
        *,
        query: str,
        instruction: str | None,
        temp_dir: Path,
        frame_limit: int,
    ) -> list[RefinedObservation]:
        remaining_run_seconds()
        observations: list[RefinedObservation] = []
        refinement_index = 0
        if request.clip_id != clip.options.clip_id:
            return observations
        crop: NormalizedCrop | None = None
        try:
            fps = (
                float(clip.options.fps)
                if clip.options.fps != "auto"
                else _auto_fps(query, clip.motion, request.fps)
            )
            timestamps = fps_timestamps(
                request.start_seconds, request.end_seconds, fps, limit=frame_limit
            )
            try:
                grounding = self._ground_crop(
                    clip,
                    request,
                    timestamps,
                    query=query,
                    instruction=instruction,
                    temp_dir=temp_dir,
                    refinement_index=refinement_index,
                )
            except Exception as grounding_exc:
                grounding = None
                status.grounding = {
                    "success": False,
                    "fallback_reason": f"grounding failed: {grounding_exc}",
                }
                logger.warning(
                    "advanced grounding failed for {}; using full-frame refinement: {}",
                    clip.options.clip_id,
                    grounding_exc,
                )
            if grounding is not None:
                status.grounding = grounding.model_dump(mode="json")
            crop = _select_refine_crop(clip.options.crop, request, grounding)
            refined_max_edge = _adaptive_refine_max_edge(query, crop)
            clip_path = self._prepare_refinement_media(
                clip,
                request,
                crop=crop,
                max_edge=refined_max_edge,
                temp_dir=temp_dir,
                refinement_index=refinement_index,
            )
        except Exception as exc:
            if crop is None:
                status.status = "failed"
                status.error = str(exc)
                return observations
            status.crop_fallback = True
            crop = None
            refined_max_edge = _adaptive_refine_max_edge(query, crop)
            try:
                clip_path = self._prepare_refinement_media(
                    clip,
                    request,
                    crop=None,
                    max_edge=refined_max_edge,
                    temp_dir=temp_dir,
                    refinement_index=refinement_index,
                )
            except Exception as fallback_exc:
                status.status = "failed"
                status.error = str(fallback_exc)
                return observations
        try:
            status.status = "completed"
            status.actual_fps.append(fps)
            status.sampled_timestamps.extend(timestamps)
            status.crop = crop
            status.refined_max_edge = refined_max_edge
            status.submitted_bytes += clip_path.stat().st_size
            video_b64 = base64.b64encode(clip_path.read_bytes()).decode("ascii")
            focused_query = (
                query if not instruction else f"{query}\nInspection focus: {instruction}"
            )
            focused_query = _refine_query(focused_query)
            video_options: dict[str, object] = {
                "detail": "high",
                "fps": round(fps, 3),
                "max_frames": frame_limit,
            }
            status.vlm_video_options = video_options
            with collect_structured_llm_traces() as refine_traces:
                facts = self._stage1_extract(
                    video_b64,
                    focused_query,
                    clip.caption,
                    (request.start_seconds, request.end_seconds),
                    video_options,
                )
            duration = request.end_seconds - request.start_seconds
            claims = []
            missing_refinement_confidence_count = 0
            for fact in facts:
                relative_start = min(max(fact.timestamp_start, 0.0), duration)
                relative_end = min(max(fact.timestamp_end, 0.0), duration)
                relative_start, relative_end = sorted((relative_start, relative_end))
                claims.append(
                    fact.model_copy(
                        update={
                            "timestamp_start": request.start_seconds + relative_start,
                            "timestamp_end": request.start_seconds + relative_end,
                        }
                    )
                )
            for claim in claims:
                confidence = claim.confidence
                if confidence is None:
                    missing_refinement_confidence_count += 1
                    continue
                if _is_uncertain_negative_claim(claim.claim):
                    confidence = min(confidence, 0.65)
                observations.append(
                    RefinedObservation(
                        observation_id=f"obs-{len(observations) + 1}",
                        clip_id=clip.options.clip_id,
                        claim=claim.claim,
                        timestamp_start=claim.timestamp_start,
                        timestamp_end=claim.timestamp_end,
                        confidence=confidence,
                        verify_reason=getattr(claim, "verify_reason", "direct Stage1 observation"),
                        fps=fps,
                        crop=crop,
                        stage="refined",
                    )
                )
            record_inspect_stage(
                tool=_TOOL_NAME,
                stage="refine",
                payload={
                    "clip_id": clip.options.clip_id,
                    "time_range": {
                        "start_seconds": request.start_seconds,
                        "end_seconds": request.end_seconds,
                    },
                    "fps": fps,
                    "refined_max_edge": refined_max_edge,
                    "vlm_video_options": video_options,
                    "timestamps": timestamps,
                    "crop": crop.model_dump(mode="json") if crop else None,
                    "grounding": grounding.model_dump(mode="json") if grounding else None,
                    "crop_fallback": status.crop_fallback,
                    "claims": [item.model_dump(mode="json") for item in claims],
                    "confidence_missing_count": missing_refinement_confidence_count,
                    "token_usage": structured_llm_trace_token_usage(refine_traces),
                },
            )
        except Exception as exc:
            status.status = "failed"
            status.error = str(exc)
            logger.warning("advanced refinement failed for {}: {}", clip.options.clip_id, exc)
        return observations

    def _ground_crop(
        self,
        clip: _PreparedClip,
        request: RefinementRequest,
        timestamps: list[float],
        *,
        query: str,
        instruction: str | None,
        temp_dir: Path,
        refinement_index: int,
    ) -> GroundedCropResult | None:
        remaining_run_seconds()
        target = _grounding_target(request, query, instruction)
        if self.grounding_cropper is None or not target:
            return None
        result = self.grounding_cropper.ground_crop(
            video_path=clip.video_path,
            target_text=target,
            timestamps=timestamps,
            temp_dir=temp_dir / f"grounding-{refinement_index:02d}",
        )
        record_inspect_stage(
            tool=_TOOL_NAME,
            stage="grounding",
            payload={
                "clip_id": clip.options.clip_id,
                "target": target,
                "time_range": {
                    "start_seconds": request.start_seconds,
                    "end_seconds": request.end_seconds,
                },
                "result": result.model_dump(mode="json"),
            },
        )
        return result

    def _prepare_refinement_media(
        self,
        clip: _PreparedClip,
        request: RefinementRequest,
        *,
        crop: NormalizedCrop | None,
        max_edge: int,
        temp_dir: Path,
        refinement_index: int,
    ) -> Path:
        remaining_run_seconds()
        clip_path = temp_dir / f"refine-{refinement_index:02d}.mp4"
        with refined_video_clip(
            clip.video_path,
            clip_path,
            start=request.start_seconds,
            end=request.end_seconds,
            crop=crop,
            max_edge=max_edge,
        ):
            pass
        return clip_path


def _required_video_part(evidence: Evidence) -> EvidencePart:
    for part in evidence.parts:
        if part.kind == EvidencePartKind.VIDEO and (part.uri or "").strip():
            if part.time_range is None:
                raise ValueError(f"evidence {evidence.id} requires a video time_range")
            return part
    raise ValueError(f"evidence {evidence.id} has no VIDEO part with uri")


def _video_range(evidence: Evidence) -> tuple[float, float]:
    part = _required_video_part(evidence)
    assert part.time_range is not None
    start, end = part.time_range.start_seconds, part.time_range.end_seconds
    if end <= start:
        raise ValueError(f"evidence {evidence.id} has an invalid time_range")
    return start, end


def _initial_status(evidence: Evidence, clip_input: AdvancedVideoClipInput) -> AdvancedClipStatus:
    return AdvancedClipStatus(
        clip_id=clip_input.clip_id,
        asset_id=str(evidence.metadata.get("asset_id") or "") or None,
        evidence_id=evidence.id,
        status="failed",
        requested_fps=clip_input.fps,
        crop=clip_input.crop,
    )


def _coarse_trace_payload(clip: _PreparedClip) -> dict:
    return {
        "clip_id": clip.options.clip_id,
        "evidence_id": clip.evidence.id,
        "time_range": {"start_seconds": clip.start, "end_seconds": clip.end},
        "timestamps": clip.coarse_timestamps,
        "motion_score": round(clip.motion, 4),
        "submitted_bytes": sum(path.stat().st_size for path in clip.coarse_frames),
    }


def _materialize_coarse_observations(
    plan: AdvancedVideoPlan, clip: _PreparedClip
) -> list[RefinedObservation]:
    timestamps = {
        f"coarse-{index:03d}": timestamp
        for index, timestamp in enumerate(clip.coarse_timestamps, start=1)
    }
    observations: list[RefinedObservation] = []
    coarse_fps = max(
        0.1,
        (len(clip.coarse_timestamps) - 1) / max(clip.end - clip.start, 0.001),
    )
    for item in plan.observations:
        if item.confidence is None:
            continue
        frame_ids = list(
            dict.fromkeys(key for key in item.supporting_frame_ids if key in timestamps)
        )
        if not frame_ids:
            continue
        supported_times = [timestamps[key] for key in frame_ids]
        start = max(clip.start, min(supported_times) - 0.05)
        end = min(clip.end, max(supported_times) + 0.05)
        if end <= start:
            end = min(clip.end, start + 0.001)
        observations.append(
            RefinedObservation(
                observation_id=f"coarse-obs-{len(observations) + 1}",
                clip_id=clip.options.clip_id,
                claim=item.claim,
                timestamp_start=start,
                timestamp_end=end,
                confidence=item.confidence,
                verify_reason=item.verify_reason or "supported by labeled coarse frames",
                fps=coarse_fps,
                crop=clip.options.crop,
                stage="coarse",
                frame_ids=frame_ids,
            )
        )
    return observations


def _validated_refinement(plan: AdvancedVideoPlan, clip: _PreparedClip) -> RefinementRequest | None:
    request = plan.refinement
    if request is None or request.clip_id != clip.options.clip_id:
        return None
    start = min(max(request.start_seconds, clip.start), clip.end)
    end = min(max(request.end_seconds, clip.start), clip.end)
    if end <= start:
        return None
    return request.model_copy(update={"start_seconds": start, "end_seconds": end})


def _fallback_full_frame_refinement(
    clip: _PreparedClip,
    *,
    query: str,
    existing: RefinementRequest | None,
) -> RefinementRequest | None:
    if clip.end <= clip.start or existing is not None:
        return None
    fps = (
        float(clip.options.fps)
        if clip.options.fps != "auto"
        else _auto_fps(query, clip.motion, 2.0)
    )
    return RefinementRequest(
        clip_id=clip.options.clip_id,
        start_seconds=clip.start,
        end_seconds=clip.end,
        fps=fps,
        crop=clip.options.crop,
        crop_confidence=1 if clip.options.crop else 0,
        grounding_target="",
        reason="full-range fallback after empty coarse observation",
    )


def _adaptive_coarse_max_edge(query: str, crop: NormalizedCrop | None) -> int:
    if crop is not None or _needs_spatial_grounding(query):
        return 1280
    return 960


def _adaptive_refine_max_edge(query: str, crop: NormalizedCrop | None) -> int:
    if crop is not None:
        area = crop.width * crop.height
        if area <= 0.12:
            return 1600
        if area <= 0.35:
            return 1440
        return 1280
    if _needs_spatial_grounding(query):
        return 1440
    return 1280


def _select_refine_crop(
    explicit_crop: NormalizedCrop | None,
    request: RefinementRequest,
    grounding: GroundedCropResult | None,
) -> NormalizedCrop | None:
    if explicit_crop is not None:
        return explicit_crop
    if grounding is not None and grounding.success and grounding.crop is not None:
        return grounding.crop
    if request.crop is not None and request.crop_confidence >= 0.7:
        return padded_crop(request.crop)
    return None


def _grounding_target(request: RefinementRequest, query: str, instruction: str | None) -> str:
    if _needs_face_expression_inspection(query):
        target = request.grounding_target.strip() or _default_grounding_target(query)
        if target and not any(term in target.lower() for term in ("face", "facial", "面部", "脸")):
            return f"{target} face"
        return target or "face of the person whose expression is asked about"
    if _needs_hand_control_panel_inspection(query):
        target = request.grounding_target.strip() or _default_grounding_target(query)
        target_lower = target.lower()
        if target and any(
            term in target_lower for term in ("hand", "finger", "手", "按钮", "旋钮", "面板")
        ):
            return target
        return "hands or fingers touching buttons, knobs, or switches on the control panel"
    if request.grounding_target.strip():
        return request.grounding_target.strip()
    if instruction and _needs_spatial_grounding(instruction):
        return instruction.strip()
    if _needs_spatial_grounding(query):
        return _default_grounding_target(query)
    return ""


def _needs_spatial_grounding(text: str) -> bool:
    text_lower = text.lower()
    return (
        any(
            term in text_lower
            for term in (
                "手",
                "按钮",
                "文字",
                "屏幕",
                "仪表",
                "面板",
                "开关",
                "旋钮",
                "控制箱",
                "控制柜",
                "手指",
                "手套",
                "小",
                "hand",
                "finger",
                "glove",
                "button",
                "knob",
                "text",
                "screen",
                "panel",
                "control box",
                "control cabinet",
                "gauge",
                "switch",
            )
        )
        or _needs_face_expression_inspection(text)
        or _needs_hand_control_panel_inspection(text)
        or _needs_text_tool_presence_inspection(text)
    )


def _needs_face_expression_inspection(text: str) -> bool:
    text_lower = text.lower()
    return any(term in text_lower for term in _FACE_EXPRESSION_TERMS)


def _needs_hand_control_panel_inspection(text: str) -> bool:
    text_lower = text.lower()
    has_hand_or_action = any(
        term in text_lower
        for term in (
            "手",
            "手指",
            "手套",
            "操作",
            "按",
            "拨",
            "转",
            "hand",
            "finger",
            "glove",
            "operate",
            "operation",
            "press",
            "push",
            "turn",
        )
    )
    has_control_target = any(
        term in text_lower
        for term in (
            "控制箱",
            "控制柜",
            "电气控制",
            "控制面板",
            "按钮",
            "旋钮",
            "开关",
            "面板",
            "control box",
            "control cabinet",
            "control panel",
            "button",
            "knob",
            "switch",
            "panel",
        )
    )
    return has_hand_or_action and has_control_target


def _needs_switch_final_state_inspection(text: str) -> bool:
    text_lower = text.lower()
    if _is_spatial_location_question(text) and not any(
        term in text_lower
        for term in (
            "复位",
            "正常位置",
            "最终状态",
            "退出",
            "reset",
            "normal position",
            "final state",
            "exit",
        )
    ):
        return False
    has_switch = any(
        term in text_lower
        for term in (
            "急停",
            "开关",
            "按钮",
            "旋钮",
            "switch",
            "button",
            "knob",
            "emergency stop",
        )
    )
    has_final_state = any(
        term in text_lower
        for term in (
            "正常位置",
            "复位",
            "退出",
            "最终",
            "转到",
            "切换",
            "弹起",
            "拉出",
            "normal position",
            "reset",
            "exit",
            "final",
            "switch to",
            "release",
            "pull",
        )
    )
    return has_switch and has_final_state


def _needs_reasoning_evidence_inspection(text: str) -> bool:
    text_lower = text.lower()
    return any(term in text_lower for term in _REASONING_EVIDENCE_TERMS)


def _needs_indicator_event_count(text: str) -> bool:
    text_lower = text.lower()
    has_indicator = any(term in text_lower for term in _INDICATOR_EVENT_TERMS)
    asks_count_or_when = any(
        term in text_lower
        for term in ("几次", "多少次", "何时", "什么时候", "when", "how many", "count")
    )
    return has_indicator and asks_count_or_when


def _needs_state_transition_inspection(text: str) -> bool:
    text_lower = text.lower()
    return any(term in text_lower for term in _STATE_TRANSITION_TERMS)


def _needs_text_tool_presence_inspection(text: str) -> bool:
    text_lower = text.lower()
    return any(term in text_lower for term in _TEXT_TOOL_PRESENCE_TERMS)


def _needs_full_temporal_refinement(text: str) -> bool:
    return (
        _needs_indicator_event_count(text)
        or _needs_state_transition_inspection(text)
        or _needs_text_tool_presence_inspection(text)
    )


def _is_spatial_location_question(text: str) -> bool:
    text_lower = text.lower()
    return any(
        term in text_lower
        for term in (
            "具体位置",
            "在哪里",
            "位于",
            "安装在",
            "位置在哪里",
            "where",
            "located",
            "location",
            "mounted",
            "installed",
        )
    )


def _default_grounding_target(query: str) -> str:
    if _needs_face_expression_inspection(query):
        return "face of the person whose expression is asked about"
    if _needs_hand_control_panel_inspection(query):
        return "hands or fingers touching buttons, knobs, or switches on the control panel"
    if _needs_indicator_event_count(query):
        return "red LED indicator lights on the circuit board"
    if _needs_text_tool_presence_inspection(query):
        return query[:120]
    if _needs_spatial_grounding(query):
        return query[:80]
    return ""


def _auto_fps(query: str, motion: float, suggested: float) -> float:
    query_lower = query.lower()
    if _needs_hand_control_panel_inspection(query):
        return min(8.0, max(4.0, suggested))
    if any(term in query_lower for term in _HIGH_FPS_TERMS):
        return min(8.0, max(6.0, suggested))
    if _needs_face_expression_inspection(query):
        return min(6.0, max(3.0, suggested))
    if any(term in query_lower for term in _FAST_MOTION_TERMS) or motion >= 0.12:
        return min(8.0, max(2.0, suggested))
    return min(1.0, max(0.5, suggested))


def _refine_query(query: str) -> str:
    query_lower = query.lower()
    additions: list[str] = []
    if _is_multiple_choice_question(query):
        additions.append(
            "For this multiple-choice question, explicitly compare the visible evidence "
            "against every option. State which option letter is supported only if the "
            "clip directly shows it. If the question premise is not visible in the clip, "
            "say that no option can be verified from this clip."
        )
    if _needs_indicator_event_count(query):
        additions.append(
            "For indicator-light timing/count questions, inspect the whole requested "
            "clip in timestamp order. Build an event list: each event must include the "
            "first visible ON frame, the last visible ON frame, and the transition back "
            "to OFF only if an OFF frame after the ON interval is directly visible. Do "
            "not compress multiple ON intervals into one event. Do not claim the light "
            "turns off at a timestamp if the light is still visibly on in that frame."
        )
    elif any(term in query_lower for term in _INDICATOR_EVENT_TERMS):
        additions.append(
            "For indicator lights, compare the labeled frames one by one and describe "
            "on/off state changes. Do not say constant on/off unless every visible frame "
            "supports that state."
        )
    if _needs_state_transition_inspection(query):
        additions.append(
            "For state-change questions, preserve temporal direction. Record the state "
            "before the change and the state after the change separately, using the "
            "timestamped frame order. Never reverse a transition such as A-to-B into "
            "B-to-A during summarization."
        )
    if _needs_text_tool_presence_inspection(query):
        additions.append(
            "For named tools, labels, or readable text such as SOP or Scotch-Brite, "
            "inspect the first, middle, and final frames of the refined window. A negative "
            "claim that the item/text/tool is absent is valid only if it is absent from "
            "the final frame and all earlier refined/context frames. If any frame contains "
            "a readable label or a plausible matching tool, describe it instead of making "
            "a broad absence claim."
        )
    if any(
        term in query_lower
        for term in ("顺时针", "逆时针", "方向", "角度", "clockwise", "counter-clockwise")
    ):
        additions.append(
            "For rotation direction or angle, infer only from visible frame-to-frame "
            "motion. If the direction or angle is blurred or ambiguous, keep it uncertain."
        )
    if _needs_face_expression_inspection(query):
        additions.append(
            "For facial expression or emotion choices, inspect the target person's face "
            "and visible body motion across frames. Compare mouth shape/opening, cheek "
            "raise, eye region, head movement, and animated gestures against every option. "
            "Do not choose 'numb' or neutral if the person is visibly speaking, laughing, "
            "smiling, or gesturing animatedly."
        )
    if _needs_hand_control_panel_inspection(query):
        additions.append(
            "For hand operation on control boxes, panels, buttons, knobs, or switches, "
            "inspect partial hands, fingers, gloves, wrists, and tools near the controls. "
            "A full worker body or face is not required to verify an operation. Do not "
            "conclude that no worker or no operation appears just because only hands or "
            "a cropped body part is visible. A negative claim is allowed only after all "
            "refined frames and context frames show no hand/finger/tool contact, approach, "
            "pressing, turning, or state change on the target control area."
        )
    if _needs_switch_final_state_inspection(query):
        additions.append(
            "For emergency-stop or switch final-state questions, distinguish pressing, "
            "releasing/popping up, rotating, pulling out, and returning to a labeled "
            "'normal' position. Do not convert a visible press or touch into a reset/normal "
            "claim unless the reset motion or final normal state is directly visible. "
            "If the video cuts away before the exit or final state, preserve uncertainty "
            "and say the exit-time order or final normal position cannot be confirmed."
        )
    if _needs_reasoning_evidence_inspection(query):
        additions.append(
            "For reasoning or evidence-choice questions, select an option only when that "
            "option's evidence is directly visible and unambiguous in the clip. Do not infer "
            "a choice from weak appearance changes, scene cuts, or context. If the requested "
            "evidence such as on-screen words, phone dates, sunglasses reflections, sun/moon "
            "changes, makeup differences, scores, or statistics is not directly visible, "
            "state that no option can be verified."
        )
    tool_detail_terms = ("材质", "直径", "棘轮", "不锈钢", "material", "diameter")
    if any(term in query_lower for term in tool_detail_terms):
        additions.append(
            "For material, diameter, ratchet/locking structures, or exact tool type, "
            "answer only if those visual details are directly visible."
        )
    if not additions:
        return query
    return "\n".join([query, *additions])


def _is_multiple_choice_question(query: str) -> bool:
    return bool(_MULTIPLE_CHOICE_RE.search(query))


def _image_content(path: Path) -> dict:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }


def _coarse_prompt(
    clip: _PreparedClip,
    query: str,
    instruction: str | None,
) -> str:
    clip_line = (
        f"- {clip.options.clip_id}: absolute range {clip.start:.3f}-{clip.end:.3f}s, "
        f"motion score {clip.motion:.3f}, caption={clip.caption!r}"
    )
    focus = f"\nAdditional focus: {instruction}" if instruction else ""
    hand_panel_lines = (
        [
            "For hand/control-panel questions, a fine inspection should target hands, "
            "fingers, gloves, buttons, knobs, switches, or the control panel area.",
            "Prefer at least 4 FPS when a quick hand action is the unresolved detail.",
        ]
        if _needs_hand_control_panel_inspection(query)
        else []
    )
    full_temporal_lines = (
        [
            "For LED timing/count, state-change, or named-text/tool presence questions, "
            "use the timestamped coarse frames as the full-range overview. Request one "
            "refinement only if they reveal a specific unresolved transition or unreadable detail.",
        ]
        if _needs_full_temporal_refinement(query)
        else []
    )
    return "\n".join(
        [
            f"Question: {query}{focus}",
            "Inspect the labeled frames from this one selected video range.",
            "Return each directly visible, question-relevant fact in observations and cite "
            "one or more exact coarse frame IDs such as coarse-001.",
            "Do not put inferred, caption-only, or option-only content in observations.",
            "If the coarse observations already capture the visible facts needed by the "
            "question, return no refinement.",
            "Only when a specific relevant detail remains unreadable or low-confidence, return one "
            "optional refinement for the smallest useful window in this same video range.",
            "The refinement times must be absolute source-video seconds inside the listed range. "
            "Do not split the video into multiple windows and do not impose a 12-second limit.",
            "Use 2-8 FPS for quick actions or ordering and 0.5-1 FPS for static appearance.",
            "Prefer grounding_target text over final crop boxes. Use crop only when obvious.",
            "Set grounding_target for small text, controls, gauges, buttons, or hand actions.",
            *hand_panel_lines,
            *full_temporal_lines,
            "Every observation must include confidence as a number from 0 to 1; "
            "do not omit the field. Use a value below 0.6 when the visual evidence "
            "is ambiguous, occluded, or incomplete.",
            "For hand laterality, count under occlusion, material, direction or unreadable text, "
            "request the smallest relevant grounding_target/crop and a useful fps. "
            "For change/size comparisons describe endpoints with their own frames and absolute "
            "times. One endpoint cannot establish a change. "
            "Report occlusion or unresolved conflict "
            "instead of filling in hidden objects. Visual flags/maps do not prove spoken mentions.",
            "Selected video:",
            clip_line,
        ]
    )


def _claim_from_observation(
    observation: RefinedObservation, claim: str, *, confidence: float | None = None
) -> AdvancedVideoClaim:
    return AdvancedVideoClaim(
        claim=claim,
        supporting_observation_ids=[observation.observation_id],
        source_clip_ids=[observation.clip_id],
        supports=[
            AdvancedClaimSupport(
                observation_id=observation.observation_id,
                stage=observation.stage,
                frame_ids=observation.frame_ids,
                clip_id=observation.clip_id,
                timestamp_start=observation.timestamp_start,
                timestamp_end=observation.timestamp_end,
                confidence=observation.confidence,
                fps=observation.fps,
                crop=observation.crop,
            )
        ],
        timestamp_start=observation.timestamp_start,
        timestamp_end=observation.timestamp_end,
        confidence=confidence
        if confidence is not None
        else _calibrated_confidence(observation.claim, observation.confidence),
        verify_reason=observation.verify_reason,
    )


def _claims_from_observations(observations: list[RefinedObservation]) -> list[AdvancedVideoClaim]:
    return [_claim_from_observation(item, item.claim) for item in observations]


def _is_uncertain_negative_claim(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _NEGATIVE_UNCERTAIN_TERMS)


def _calibrated_confidence(claim: str, confidence: float) -> float:
    if _is_uncertain_negative_claim(claim):
        return min(confidence, 0.65)
    return confidence
