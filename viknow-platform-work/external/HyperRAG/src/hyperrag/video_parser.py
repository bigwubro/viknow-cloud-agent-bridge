"""
VideoParser: 基于定长时间窗的基础视频解析器（v1）

特性（按当前实施顺序）：
- 无场景分割（后续可选接入 PySceneDetect）
- 定长分段（video_segment_length_sec）
- 内置 ASR（OpenAI 兼容），使用 silero-vad 做语音段切分，可被 asr_model_func 覆盖
- 可选 VLM 增强（vlm_model_func），结合上下文提示词
- 缓存（index.json 为 assets+segments 快照；frames/{segment_id}.jpg 代表帧）

注意：
- 仅输出 type="video_clip" 的段；帧仅用于 VLM 推理与代表帧落盘
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import tqdm
from moviepy.video.io.VideoFileClip import VideoFileClip
from openai import OpenAI

from .audio_transcriber import SileroVADAudioTranscriber, TranscriptionConfig, VADConfig
from .schema import Asset, Segment

logger = logging.getLogger(__name__)

def default_asr_model_func(
    client: OpenAI,
    video_path: str,
    lang: str = "zh",
    hotwords: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:

    if hotwords:
        hotwords = "需要注意以下术语:\n" + ", ".join(hotwords)

    with open(video_path, "rb") as f:
        transcription = client.audio.transcriptions.create(
            file=f,
            model="vllm/whisper-large-v3",
            language=lang,
            response_format="verbose_json",
            prompt=hotwords,
        )

    results: List[Dict] = []

    if transcription.segments is None:
        logger.warning(f"VideoParser: no segments found in transcription: {transcription}")
        return results

    for s in transcription.segments:
        results.append(
            {
                "begin_ts": s.start,
                "end_ts": s.end,
                "text": s.text,
            }
        )

    return results


class VideoParser:
    """视频解析器（v1）。

    parse_document 返回 (Asset, List[Segment])，其中 Segment.type 均为 "video_clip"。
    """
    BRIDGE_MAX_GAP_SEC = 1.0
    TERMINAL_PUNCTUATION = ("。", "！", "？", ".", "!", "?", ";", "；")

    def __init__(self, asr_model_func, video_caption_func: Optional[Callable] = None, cache_root_dir: Optional[Union[str, Path]] = None, max_vlm_concurrent: int = 5, max_asr_concurrent: int = 8):
        self.client = OpenAI(**(asr_model_func or {}))
        self.asr_model_func = default_asr_model_func
        self.audio_transcriber = SileroVADAudioTranscriber(
            asr_callable=self._run_asr_for_audio_clip,
            config=TranscriptionConfig(
                vad=VADConfig(),
                max_concurrent_asr=max(1, max_asr_concurrent),
            ),
        )
        # 视频片段描述函数（路径 + prompt -> caption 文本）
        self.video_caption_func = video_caption_func
        self.cache_root_dir = Path(cache_root_dir) if cache_root_dir is not None else Path(".video_cache")
        # VLM 并发控制：最大并发数
        self.max_vlm_concurrent = max(1, max_vlm_concurrent)

    SUPPORTED_FORMATS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm"}

    def parse_document(
        self,
        doc_id: str,
        video_path: Union[str, Path],
        parser_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Asset, List[Segment]]:
        # ---------- 准备与参数 ----------
        video_path = Path(video_path)
        # 选择缓存根：优先入参，其次对象级，最后临时默认（视频同级 .video_cache）
        
        if not video_path.exists():
            raise FileNotFoundError(f"Video file does not exist: {video_path}")

        params = self._normalize_params(parser_params)
        terminology_prompt_context = None
        try:
            terminology_prompt_context = (parser_params or {}).get("terminology_prompt_context")
        except Exception:
            terminology_prompt_context = None
        override_title = None
        try:
            override_title = (parser_params or {}).get("title")
        except Exception:
            override_title = None

        # 缓存目录与文件
        video_cache_dir = self.cache_root_dir / video_path.stem
        frames_dir = video_cache_dir / "frames"
        video_cache_dir.mkdir(parents=True, exist_ok=True)
        frames_dir.mkdir(parents=True, exist_ok=True)

        file_hash = self._calculate_file_hash(video_path)
        index_file = video_cache_dir / "index.json"

        # ---------- 媒体探测与时间窗 ----------
        probe = self._ffprobe(video_path)
        duration_sec, width, height, fps, creation_time = self._extract_media_meta(probe)
        has_audio_stream = self._has_audio_stream(probe)
        windows = self._build_windows(duration_sec, params["segment_length_sec"])
        seg_ids = [f"{doc_id}_clip_{i:04d}" for i in range(len(windows))]
        # 生成 segment_id -> video_uuid 映射
        self.seg_uuid_map = {
            seg_id: f"viknow-{doc_id}-{seg_id}"
            for seg_id in seg_ids
        }

        clips_dir = video_cache_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        # 构建 index -> segment_name 映射，并导出剪辑文件
        try:
            with VideoFileClip(video_path) as video:
                for index in tqdm.tqdm(range(len(windows)), desc=f"Saving Video Segments {video_path}"):
                    start, end = windows[index]
                    if end > video.duration:
                        end = video.duration
                    video_file = f'{seg_ids[index]}{video_path.suffix}'
                    clip_path = clips_dir / video_file
                    subvideo = video.subclipped(start, end)
                    subvideo.write_videofile(
                        str(clip_path),
                        temp_audiofile_path=str(clips_dir), 
                        remove_temp=True,
                        codec='libx264', 
                        logger=None)
                    self._ensure_clip_has_min_frames(clip_path, min_frames=2)
        except Exception as e:
            logger.exception(f"VideoParser: failed to save video segments: {e}")

        # ---------- ASR ----------
        # TODO(scene-detect): 若启用场景分割，先生成 scenes.json，再在场景内做定长切分
        # TODO(config): 从配置项读取默认语言/热词，允许通过 parser_params 覆盖更细粒度参数
        # TODO(emb-cache): 未来在此处对音频或片段向量做缓存以加速
        # 从视频中提取音频，保存在cache目录下，使用音频路径调用asr
        raw_asr_segments: List[Dict[str, Any]] = []
        if has_audio_stream:
            audio_path = video_cache_dir / "audio.wav"
            self._extract_audio(video_path, audio_path)
            self.audio_transcriber = SileroVADAudioTranscriber(
                asr_callable=self._run_asr_for_audio_clip,
                config=TranscriptionConfig(
                    vad=VADConfig(),
                    max_concurrent_asr=self.audio_transcriber.config.max_concurrent_asr,
                    asr_lang=params["asr_lang"] or "zh",
                    asr_hotwords=params["asr_hotwords"],
                ),
            )
            raw_asr_segments = self.audio_transcriber.transcribe_file(audio_path)
        else:
            logger.info(f"VideoParser: no audio stream detected, skip ASR for {video_path}")

        transcript_file = video_cache_dir / "transcript.json"
        self._write_json_silent(transcript_file, raw_asr_segments)

        # ---------- 聚合为 clip 段并可选 VLM 增强（并发执行）----------
        # 1. 先收集所有窗口的基础文本
        base_texts: List[str] = []
        for i, (begin_ts, end_ts) in enumerate(windows):
            text_joined = self._collect_texts_for_window(raw_asr_segments, begin_ts, end_ts)
            base_texts.append(text_joined)

        # 对跨窗口的半句进行桥接：将完整句补到前后两个窗口中，增强相邻片段语义连续性。
        base_texts = self._apply_cross_window_sentence_bridges(raw_asr_segments, windows, base_texts)

        # 2. 并发执行所有 VLM 增强任务
        vlm_results = asyncio.run(self._enhance_all_clips_with_vlm(
            windows=windows,
            seg_ids=seg_ids,
            seg_uuid_map=self.seg_uuid_map,
            clips_dir=clips_dir,
            video_suffix=video_path.suffix,
            base_texts=base_texts,
            duration_sec=duration_sec,
            fps=fps,
            width=width,
            height=height,
            clip_sample_fps=params["clip_sample_fps"],
            terminology_prompt_context=terminology_prompt_context,
        ))

        # 3. 组装最终的 Segment 列表
        clip_segments: List[Segment] = []
        for i, (begin_ts, end_ts) in enumerate(windows):
            seg_id = seg_ids[i]
            text_joined, vlm_extra = vlm_results[i]

            seg = Segment(
                segment_id=seg_id,
                doc_id=doc_id,
                order=i,
                type="video_clip",
                text=text_joined,
                location={"begin_ts": begin_ts, "end_ts": end_ts},
                extra=vlm_extra,
            )
            clip_segments.append(seg)

        # ---------- 生成 Asset 并写缓存 ----------
        duration_seconds = max(0, int(round(duration_sec)))
        avg_fps_rounded = max(0, int(round(fps)))
        creation_time_display = creation_time or "unknown"
        global_metadata = (
            f"时长:{duration_seconds}秒；平均帧率:{avg_fps_rounded}fps；创建时间:{creation_time_display}"
        )

        asset = Asset(
            doc_id=doc_id,
            source_uri=str(video_path),
            modality="video",
            segments_count=len(clip_segments),
            title=str(override_title) if override_title else video_path.stem,
            language=params["asr_lang"],
            extra={
                "file_hash": file_hash,
                "video_cache_dir": str(video_cache_dir),
                "frames_dir": str(frames_dir),
                "transcript_file": str(transcript_file),
                "duration_sec": duration_sec,
                "fps": fps,
                "resolution": [width, height],
                "global_metadata": global_metadata,
            },
        )

        self._save_cached_result(index_file, doc_id, file_hash, asset, clip_segments)
        return asset, clip_segments

    # ---------------------- 解析子步骤 ----------------------
    @staticmethod
    def _normalize_params(pp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        p = dict(pp or {})
        return {
            "segment_length_sec": int(p.get("segment_length_sec", 30)),
            "clip_sample_fps": float(p.get("clip_sample_fps", 1.0)),
            "asr_lang": p.get("asr_lang"),
            "asr_hotwords": list(p.get("asr_hotwords", []) or []),
        }

    def _load_cached_result(self, index_file: Path, file_hash: str) -> Optional[Tuple[Asset, List[Segment]]]:
        if not index_file.exists():
            return None
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("file_hash") == file_hash:
                logger.info("VideoParser: cache hit")
                asset = Asset(**cached["asset"])  # type: ignore[arg-type]
                segments = [Segment(**s) for s in cached["segments"]]  # type: ignore[list-item]
                return asset, segments
        except Exception as e:
            logger.warning(f"VideoParser: failed to load cache index.json: {e}")
        return None

    @staticmethod
    def _save_cached_result(index_file: Path, doc_id: str, file_hash: str, asset: Asset, segments: List[Segment]) -> None:
        try:
            with open(index_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "doc_id": doc_id,
                        "file_hash": file_hash,
                        "asset": asset.model_dump(),
                        "segments": [s.model_dump() for s in segments],
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning(f"VideoParser: failed to write cache index.json: {e}")

    @staticmethod
    def _extract_media_meta(probe: Dict[str, Any]) -> Tuple[float, int, int, float, Optional[str]]:
        duration_sec = float(probe.get("format", {}).get("duration", 0) or 0)
        video_stream = None
        for s in probe.get("streams", []):
            if s.get("codec_type") == "video":
                video_stream = s
                break
        width = int((video_stream or {}).get("width", 0) or 0)
        height = int((video_stream or {}).get("height", 0) or 0)
        avg_frame_rate = (video_stream or {}).get("avg_frame_rate")
        r_frame_rate = (video_stream or {}).get("r_frame_rate")
        fps = VideoParser._parse_fps(avg_frame_rate or r_frame_rate or "0/1")

        format_tags = probe.get("format", {}).get("tags", {})
        stream_tags = (video_stream or {}).get("tags", {})
        creation_time = format_tags.get("creation_time") or stream_tags.get("creation_time")
        if creation_time is not None:
            creation_time = str(creation_time)

        return duration_sec, width, height, fps, creation_time

    @staticmethod
    def _has_audio_stream(probe: Dict[str, Any]) -> bool:
        for s in probe.get("streams", []):
            if s.get("codec_type") == "audio":
                return True
        return False

    @staticmethod
    def _build_windows(duration_sec: float, segment_len: int) -> List[Tuple[float, float]]:
        if duration_sec <= 0:
            return [(0.0, float(segment_len))]
        windows: List[Tuple[float, float]] = []
        num = int(math.ceil(duration_sec / max(1, segment_len)))
        for i in range(num):
            b = float(i * segment_len)
            e = float(min(duration_sec, (i + 1) * segment_len))
            if e > b:
                windows.append((b, e))
        return windows

    @staticmethod
    def _write_json_silent(path: Path, data: Any) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"VideoParser: failed to write json file: {path} \n {e}")

    @staticmethod
    def _collect_texts_for_window(
        raw_asr_segments: List[Dict[str, Any]],
        begin_ts: float,
        end_ts: float,
    ) -> str:
        texts: List[str] = []
        for r in raw_asr_segments:
            rb = float(r.get("begin_ts", 0) or 0)
            re = float(r.get("end_ts", 0) or 0)
            if re > begin_ts and rb < end_ts:
                t = str(r.get("text", "")).strip()
                if t:
                    texts.append(t)
        return " \n".join(texts).strip()

    @classmethod
    def _apply_cross_window_sentence_bridges(
        cls,
        raw_asr_segments: List[Dict[str, Any]],
        windows: List[Tuple[float, float]],
        base_texts: List[str],
    ) -> List[str]:
        if len(windows) <= 1 or not raw_asr_segments:
            return base_texts

        bridged_texts = list(base_texts)
        ordered = sorted(
            raw_asr_segments,
            key=lambda x: (float(x.get("begin_ts", 0) or 0), float(x.get("end_ts", 0) or 0)),
        )

        for i in range(len(windows) - 1):
            boundary_ts = windows[i][1]
            left = cls._find_nearest_left_asr_segment(ordered, boundary_ts)
            right = cls._find_nearest_right_asr_segment(ordered, boundary_ts)

            if left is None or right is None:
                continue

            # 若本来就有单个 ASR 片段跨过边界，窗口重叠规则已经可覆盖，无需桥接。
            if cls._has_crossing_segment(ordered, boundary_ts):
                continue

            left_text = str(left.get("text", "")).strip()
            right_text = str(right.get("text", "")).strip()
            if not left_text or not right_text:
                continue

            if left_text.endswith(cls.TERMINAL_PUNCTUATION):
                continue

            left_end = float(left.get("end_ts", 0) or 0)
            right_begin = float(right.get("begin_ts", 0) or 0)
            if boundary_ts - left_end > cls.BRIDGE_MAX_GAP_SEC:
                continue
            if right_begin - boundary_ts > cls.BRIDGE_MAX_GAP_SEC:
                continue

            merged_sentence = f"{left_text}{right_text}"

            if merged_sentence not in bridged_texts[i]:
                bridged_texts[i] = (
                    f"{bridged_texts[i]} \n{merged_sentence}".strip()
                    if bridged_texts[i]
                    else merged_sentence
                )

            if merged_sentence not in bridged_texts[i + 1]:
                bridged_texts[i + 1] = (
                    f"{merged_sentence} \n{bridged_texts[i + 1]}".strip()
                    if bridged_texts[i + 1]
                    else merged_sentence
                )

        return bridged_texts

    @staticmethod
    def _find_nearest_left_asr_segment(
        ordered_segments: List[Dict[str, Any]], boundary_ts: float
    ) -> Optional[Dict[str, Any]]:
        left: Optional[Dict[str, Any]] = None
        for seg in ordered_segments:
            end_ts = float(seg.get("end_ts", 0) or 0)
            if end_ts <= boundary_ts:
                left = seg
                continue
            break
        return left

    @staticmethod
    def _find_nearest_right_asr_segment(
        ordered_segments: List[Dict[str, Any]], boundary_ts: float
    ) -> Optional[Dict[str, Any]]:
        for seg in ordered_segments:
            begin_ts = float(seg.get("begin_ts", 0) or 0)
            if begin_ts >= boundary_ts:
                return seg
        return None

    @staticmethod
    def _has_crossing_segment(
        ordered_segments: List[Dict[str, Any]], boundary_ts: float
    ) -> bool:
        for seg in ordered_segments:
            begin_ts = float(seg.get("begin_ts", 0) or 0)
            end_ts = float(seg.get("end_ts", 0) or 0)
            if begin_ts < boundary_ts < end_ts:
                return True
        return False

    async def _enhance_all_clips_with_vlm(
        self,
        windows: List[Tuple[float, float]],
        seg_ids: List[str],
        seg_uuid_map: Dict[str, str],
        clips_dir: Path,
        video_suffix: str,
        base_texts: List[str],
        duration_sec: float,
        fps: float,
        width: int,
        height: int,
        clip_sample_fps: float,
        terminology_prompt_context: Optional[str] = None,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """并发执行所有片段的 VLM 增强。
        
        返回：List[(enhanced_text, extra_dict)]，与输入的 windows 顺序一致
        """
        if not callable(self.video_caption_func):
            # 如果没有 VLM 函数，直接返回原始文本
            return [(text, {}) for text in base_texts]

        # 创建信号量控制并发数
        semaphore = asyncio.Semaphore(self.max_vlm_concurrent)

        # 创建所有异步任务
        tasks = []
        for i, (begin_ts, end_ts) in enumerate(windows):
            seg_id = seg_ids[i]
            video_path = str(clips_dir / f'{seg_id}{video_suffix}')
            base_text = base_texts[i]
            logger.info(f"VideoParser: starting VLM enhancement for {video_path}")
            task = self._maybe_enhance_with_vlm_async(
                semaphore=semaphore,
                video_path=video_path,
                begin_ts=begin_ts,
                end_ts=end_ts,
                uuid=seg_uuid_map.get(seg_id, ""),
                duration_sec=duration_sec,
                fps=fps,
                width=width,
                height=height,
                base_text=base_text,
                clip_sample_fps=clip_sample_fps,
                terminology_prompt_context=terminology_prompt_context,
            )
            logger.info(f"VideoParser: added task for {video_path}")
            tasks.append(task)

        # 并发执行所有任务（受信号量限制）
        logger.info(f"VideoParser: starting concurrent VLM enhancement for {len(tasks)} clips (max concurrent: {self.max_vlm_concurrent})")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理异常情况
        final_results: List[Tuple[str, Dict[str, Any]]] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"VideoParser: VLM enhancement failed for clip {i}: {result}")
                final_results.append((base_texts[i], {}))
            else:
                final_results.append(result)
        
        return final_results

    async def _maybe_enhance_with_vlm_async(
        self,
        semaphore: asyncio.Semaphore,
        video_path: str,
        begin_ts: float,
        end_ts: float,
        uuid: str,
        duration_sec: float,
        fps: float,
        width: int,
        height: int,
        base_text: str,
        clip_sample_fps: float,
        terminology_prompt_context: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """异步执行单个片段的 VLM 增强。"""
        if not callable(self.video_caption_func):
            return base_text, {}

        # TODO(prompt): 将提示词模板抽离到可配置模块，便于统一调整与 A/B 实验
        # 构造英文提示词，要求中文输出；包含时间范围与 ASR 文本作为参考
        prompt_parts: List[str] = [
            "You are a helpful assistant that can analyze video and provide captions. Analyze the video and provide a detailed technical report in a structured, objective format, based on following requiremnets:",
            "1. First, create a bulleted list of all objects, their key attributes (color, size, material, state(moving/steady)) if legible, and their interconnections. Then, using only this list, generate a detailed paragraph-long caption that synthesizes these observations. ",
            "2. Second, OCR task. create a bulleted list of all text in the video, including their text content, color, and location based on object(e.g., on the monitor screen, as video watermark, etc.).",
            "3. Third, describe the sequence of actions that occur in this video. Begin with the first action, and describe the outcome of each subsequent action.",
            "4. Output language: Chinese.",
            f"Video timestamps: {self._fmt_ts(begin_ts)} - {self._fmt_ts(end_ts)}",
        ]
        if terminology_prompt_context:
            tctx = str(terminology_prompt_context).strip()
            if tctx:
                prompt_parts.append(
                    "Terminology (authoritative, use these meanings when relevant; do not invent beyond them):\n"
                    + tctx
                )
        if base_text:
            prompt_parts.append("Transcript (ASR):\n" + base_text)
        prompt = "\n".join(prompt_parts)

        # 使用信号量控制并发
        async with semaphore:
            try:
                caption = await self.video_caption_func(video_path, begin_ts, end_ts, prompt, uuid=uuid)
                logger.info(f"VideoParser: VLM caption for {video_path}: {caption[:100]}...")
            except Exception as e:
                logger.warning(f"VideoParser: VLM error for {video_path}: {e}")
                return base_text, {}

        if not isinstance(caption, str):
            logger.warning(f"VideoParser: VLM output is not a string: {caption}")
            return base_text, {}

        merged = (base_text + "\n[视觉要点]\n" + caption.strip()) if base_text else ("[视觉要点]\n" + caption.strip())
        return merged, {}

    @staticmethod
    def _fmt_ts(ts: float) -> str:
        try:
            t = max(0, int(float(ts)))
            mm, ss = divmod(t, 60)
            hh, mm = divmod(mm, 60)
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        except Exception:
            return "00:00:00"

    # ---------------------- 工具方法 ----------------------
    @staticmethod
    def _calculate_file_hash(path: Path) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"

    @staticmethod
    def _ffprobe(path: Path) -> Dict[str, Any]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(res.stdout or "{}")
        except FileNotFoundError:
            raise RuntimeError("ffprobe not found. Please install ffmpeg.")
        except subprocess.CalledProcessError as e:
            logger.error(f"ffprobe error: {e.stderr}")
            return {}
        except Exception:
            return {}

    def _ensure_clip_has_min_frames(self, clip_path: Path, min_frames: int = 2) -> None:
        frame_count = self._get_video_frame_count(clip_path)
        if frame_count >= min_frames:
            return
        if frame_count <= 0:
            logger.warning(f"VideoParser: skip frame padding due to invalid frame count for clip: {clip_path}")
            return

        frames_to_pad = min_frames - frame_count
        logger.warning(
            f"VideoParser: clip has only {frame_count} frame(s), padding {frames_to_pad} frame(s): {clip_path}"
        )
        self._pad_clip_with_cloned_last_frame(clip_path, frames_to_pad)

        fixed_count = self._get_video_frame_count(clip_path)
        if fixed_count < min_frames:
            logger.warning(
                f"VideoParser: clip frame padding did not reach target ({fixed_count} < {min_frames}): {clip_path}"
            )

    @staticmethod
    def _get_video_frame_count(clip_path: Path) -> int:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames,nb_frames",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(clip_path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            counts: List[int] = []
            for line in (res.stdout or "").splitlines():
                value = line.strip()
                if not value or value == "N/A":
                    continue
                counts.append(int(float(value)))
            return max(counts) if counts else 0
        except FileNotFoundError:
            raise RuntimeError("ffprobe not found. Please install ffmpeg.")
        except subprocess.CalledProcessError as e:
            logger.warning(f"VideoParser: failed to count frames for {clip_path}: {e.stderr}")
            return 0
        except Exception as e:
            logger.warning(f"VideoParser: failed to count frames for {clip_path}: {e}")
            return 0

    @staticmethod
    def _pad_clip_with_cloned_last_frame(clip_path: Path, frames_to_pad: int) -> None:
        tmp_path = clip_path.with_name(f"{clip_path.stem}.tmp{clip_path.suffix}")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(clip_path),
            "-map",
            "0:v:0",
            "-vf",
            f"tpad=stop_mode=clone:stop={frames_to_pad}",
            "-an",
            str(tmp_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            tmp_path.replace(clip_path)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found. Please install ffmpeg.")
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            logger.exception(f"VideoParser: failed to pad single-frame clip {clip_path}: {e}")
            raise
    
    @staticmethod
    def _extract_audio(video_path: Path, audio_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            str(audio_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found. Please install ffmpeg.")

    def _run_asr_for_audio_clip(
        self,
        audio_path: str,
        lang: Optional[str],
        hotwords: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        return self.asr_model_func(
            self.client,
            audio_path,
            lang=lang or "zh",
            hotwords=hotwords,
        )

    @staticmethod
    def _parse_fps(r_frame_rate: str) -> float:
        try:
            if "/" in r_frame_rate:
                a, b = r_frame_rate.split("/", 1)
                a = float(a)
                b = float(b) if float(b) != 0 else 1.0
                return a / b
            return float(r_frame_rate)
        except Exception:
            return 0.0

    @staticmethod
    def _extract_frame(video_path: Path, ts_sec: float, out_path: Path) -> None:
        # 取最近关键帧附近一帧
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(max(0.0, ts_sec)),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found. Please install ffmpeg.")

    @staticmethod
    def _extract_frames_batch(
        video_path: Path, begin_ts: float, end_ts: float, fps: float, out_dir: Path
    ) -> List[str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        duration = max(0.0, end_ts - begin_ts)
        if duration <= 0 or fps <= 0:
            return []
        # 使用 -ss/-t 裁剪窗口再采样 fps 帧，文件名按序号输出
        # 注意：对长片段此操作仍可能较慢，调用方应控制 fps 与窗口时长
        pattern = out_dir / "frame_%05d.jpg"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(max(0.0, begin_ts)),
            "-t",
            str(duration),
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps}",
            "-q:v",
            "2",
            str(pattern),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found. Please install ffmpeg.")
        # 收集生成的帧路径
        return [str(p) for p in sorted(out_dir.glob("frame_*.jpg"))]
