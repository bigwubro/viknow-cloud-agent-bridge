from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, request


class OpenAICompatibleMultimodalEmbedder:
    """OpenAI-compatible multimodal embedding client for text, image, and video."""

    REQUIRED_TASK_KEYS = (
        "video_indexing",
        "query_text_to_video",
        "query_image_to_video",
    )

    DEFAULT_TASK_INSTRUCTIONS = {
        "video_indexing": (
            "Encode this video segment for semantic retrieval, emphasizing actions, "
            "objects, and temporal cues."
        ),
        "query_text_to_video": (
            "Encode this query for retrieving the most semantically aligned instructional "
            "video segment."
        ),
        "query_image_to_video": (
            "Encode this image for retrieving the most semantically aligned instructional "
            "video segment."
        ),
    }

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_sec: float = 120.0,
        provider: str = "vllm",
        local_video_mode: str = "video_base64",
        max_inline_image_bytes: int = 10 * 1024 * 1024,
        max_inline_video_bytes: int = 30 * 1024 * 1024,
        instruction_default: str = "Represent the user's input.",
        instruction_map: Optional[Dict[str, str]] = None,
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "").strip()
        self.timeout_sec = float(timeout_sec)
        self.provider = str(provider or "vllm").strip()
        self.local_video_mode = str(local_video_mode or "video_base64").strip().lower()
        self.max_inline_image_bytes = int(max_inline_image_bytes)
        self.max_inline_video_bytes = int(max_inline_video_bytes)
        self.instruction_default = str(instruction_default or "Represent the user's input.")
        self.instruction_map = instruction_map or self._default_instruction_map()

        if not self.model:
            raise ValueError("multimodal embedding model is required")

        allowed_modes = {"video_base64", "first_frame_image", "url_only"}
        if self.local_video_mode not in allowed_modes:
            raise ValueError(
                f"Unsupported local_video_mode={self.local_video_mode}, expected one of {sorted(allowed_modes)}"
            )

    @classmethod
    def _default_instruction_map(cls) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for key in cls.REQUIRED_TASK_KEYS:
            value = str(cls.DEFAULT_TASK_INSTRUCTIONS.get(key, "")).strip()
            if not value:
                raise ValueError(f"Missing required multimodal embedding instruction: {key}")
            result[key] = value
        return result

    @staticmethod
    def _normalize_base_url(raw_base_url: str) -> str:
        base = str(raw_base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("multimodal embedding base_url is required")
        if base.endswith("/embeddings"):
            base = base[: -len("/embeddings")]
        if "/v1/" in base:
            return base.split("/v1/")[0] + "/v1"
        if base.endswith("/v1"):
            return base
        return base + "/v1"

    @staticmethod
    def _is_remote_or_data_url(value: str) -> bool:
        normalized = str(value or "").strip().lower()
        return (
            normalized.startswith("http://")
            or normalized.startswith("https://")
            or normalized.startswith("data:")
        )

    @staticmethod
    def _infer_mime_type(path: str, default: str) -> str:
        guessed, _ = mimetypes.guess_type(path)
        return guessed or default

    def _resolve_instruction(self, task: Optional[str], instruction: Optional[str]) -> str:
        if instruction is not None:
            return str(instruction)
        task_key = str(task or "").strip()
        if task_key and task_key in self.instruction_map:
            return self.instruction_map[task_key]
        return self.instruction_default

    def _transcode_video_for_inline(self, source_path: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            prefix="hyperrag_inline_", suffix=".mp4", delete=False
        ) as tmp:
            output_path = Path(tmp.name)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            (
                "scale="
                "'trunc(iw*min(1,min(1280/iw,720/ih))/2)*2':"
                "'trunc(ih*min(1,min(1280/iw,720/ih))/2)*2'"
            ),
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-an",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
            if output_path.exists():
                output_path.unlink()
            error = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
            raise RuntimeError(
                f"Failed to transcode video for inline embedding: {source_path}; error={error}"
            )
        return output_path

    @staticmethod
    def _video_frame_count(path: Path) -> int:
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
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return 0
        counts = []
        for line in (result.stdout or "").splitlines():
            value = line.strip()
            if not value or value == "N/A":
                continue
            counts.append(int(float(value)))
        return max(counts) if counts else 0

    def _pad_single_frame_video(self, source_path: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            prefix="hyperrag_padded_", suffix=".mp4", delete=False
        ) as tmp:
            output_path = Path(tmp.name)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-vf",
            "tpad=stop_mode=clone:stop=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-an",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
            if output_path.exists():
                output_path.unlink()
            error = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
            raise RuntimeError(
                f"Failed to pad local video for embedding: {source_path}; error={error}"
            )
        return output_path

    def _prepare_local_video(self, path: str) -> tuple[Path, Optional[Path]]:
        source_path = Path(path)
        if not source_path.exists():
            raise FileNotFoundError(f"Media file not found: {source_path}")
        if self._video_frame_count(source_path) != 1:
            return source_path, None
        padded = self._pad_single_frame_video(source_path)
        return padded, padded

    def _file_to_data_url(self, path: str, default_mime: str, max_inline_bytes: int) -> str:
        fp = Path(path)
        if not fp.exists():
            raise FileNotFoundError(f"Media file not found: {fp}")
        size = fp.stat().st_size
        data_source = fp
        cleanup_path: Optional[Path] = None
        if size > max_inline_bytes and default_mime.startswith("video/"):
            data_source = self._transcode_video_for_inline(fp)
            cleanup_path = data_source
            size = data_source.stat().st_size
        if size > max_inline_bytes:
            if cleanup_path and cleanup_path.exists():
                cleanup_path.unlink()
            raise ValueError(
                f"Media file too large for inline base64 ({size} > {max_inline_bytes}): {data_source}"
            )
        try:
            mime_type = self._infer_mime_type(str(data_source), default_mime)
            payload = base64.b64encode(data_source.read_bytes()).decode("utf-8")
            return f"data:{mime_type};base64,{payload}"
        finally:
            if cleanup_path and cleanup_path.exists():
                cleanup_path.unlink()

    def _video_first_frame_to_data_url(self, path: str) -> str:
        import cv2

        fp = Path(path)
        if not fp.exists():
            raise FileNotFoundError(f"Media file not found: {fp}")
        cap = cv2.VideoCapture(str(fp))
        if not cap.isOpened():
            raise ValueError(f"Unable to open video file: {fp}")
        try:
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if frame_count > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_count // 2)
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok or frame is None:
                raise ValueError(f"Unable to decode video frame: {fp}")
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                raise ValueError(f"Unable to encode video frame: {fp}")
            raw = encoded.tobytes()
            if len(raw) > self.max_inline_image_bytes:
                raise ValueError(
                    f"Extracted frame too large ({len(raw)} > {self.max_inline_image_bytes}): {fp}"
                )
            payload = base64.b64encode(raw).decode("utf-8")
            return f"data:image/jpeg;base64,{payload}"
        finally:
            cap.release()

    def _post_embeddings(
        self, messages: list[dict[str, Any]], *, max_attempts: int = 3
    ) -> list[float]:
        url = f"{self.base_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "messages": messages,
            "model": self.model,
            "encoding_format": "float",
            "continue_final_message": True,
            "add_special_tokens": True,
        }
        raw_body = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            req = request.Request(url, data=raw_body, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=self.timeout_sec) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                break
            except error.HTTPError as exc:
                exc.close()
                last_error = exc
                if exc.code < 500 and exc.code != 429:
                    raise
            except (OSError, error.URLError) as exc:
                last_error = exc
            if attempt == max_attempts:
                raise last_error
            time.sleep(min(8.0, 2 ** (attempt - 1)))
        else:
            raise RuntimeError("unreachable")
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"Invalid embeddings response: {body}")
        embedding = data[0].get("embedding")
        if not isinstance(embedding, list):
            raise RuntimeError(f"Missing embedding vector in response: {body}")
        try:
            from hyperrag.observability.index_usage import record_generation

            usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
            if usage:
                record_generation(
                    name="multimodal_embedding",
                    model=self.model,
                    usage=usage,
                )
        except Exception:
            pass
        return embedding

    def _build_text_messages(self, text: str, instruction: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {"role": "user", "content": [{"type": "text", "text": str(text or "")}]},
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ]

    def _build_image_messages(self, image_path: str, instruction: str) -> list[dict[str, Any]]:
        source = str(image_path or "").strip()
        if not source:
            raise ValueError("image_path is required")
        image_url = (
            source
            if self._is_remote_or_data_url(source)
            else self._file_to_data_url(
                source,
                default_mime="image/jpeg",
                max_inline_bytes=self.max_inline_image_bytes,
            )
        )
        return [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": ""},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ]

    def _build_image_text_messages(
        self,
        *,
        image_url: str,
        text: str,
        instruction: str,
    ) -> list[dict[str, Any]]:
        normalized_image_url = str(image_url or "").strip()
        if not normalized_image_url:
            raise ValueError("image_url is required")
        normalized_text = str(text or "").strip()
        return [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": normalized_image_url}},
                    {"type": "text", "text": normalized_text},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ]

    def _build_video_messages(
        self,
        video_path: str,
        instruction: str,
        text: str = "",
    ) -> list[dict[str, Any]]:
        source = str(video_path or "").strip()
        semantic_text = str(text or "").strip()
        if not source:
            raise ValueError("video_path is required")
        if self._is_remote_or_data_url(source):
            content = [
                {"type": "video_url", "video_url": {"url": source}},
                {"type": "text", "text": semantic_text},
            ]
        else:
            prepared_path, cleanup_path = self._prepare_local_video(source)
            try:
                if self.local_video_mode == "video_base64":
                    video_url = self._file_to_data_url(
                        str(prepared_path),
                        default_mime="video/mp4",
                        max_inline_bytes=self.max_inline_video_bytes,
                    )
                    content = [
                        {"type": "video_url", "video_url": {"url": video_url}},
                        {"type": "text", "text": semantic_text},
                    ]
                elif self.local_video_mode == "first_frame_image":
                    image_url = self._video_first_frame_to_data_url(str(prepared_path))
                    content = [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {
                            "type": "text",
                            "text": (
                                "A representative keyframe from a local video segment.\n"
                                f"{semantic_text}"
                            ).strip(),
                        },
                    ]
                else:
                    raise ValueError(
                        "local_video_mode=url_only requires a remote video url or pre-built data url"
                    )
            finally:
                if cleanup_path and cleanup_path.exists():
                    cleanup_path.unlink()
        return [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {"role": "user", "content": content},
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ]

    async def embed_text(
        self, text: str, *, task: Optional[str] = None, instruction: Optional[str] = None
    ) -> list[float]:
        final_instruction = self._resolve_instruction(task=task, instruction=instruction)
        messages = self._build_text_messages(text=str(text or ""), instruction=final_instruction)
        return await asyncio.to_thread(self._post_embeddings, messages)

    async def embed_image(
        self,
        image_path: str,
        *,
        task: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> list[float]:
        final_instruction = self._resolve_instruction(task=task, instruction=instruction)
        messages = self._build_image_messages(image_path=image_path, instruction=final_instruction)
        return await asyncio.to_thread(self._post_embeddings, messages)

    def embed_video_sync(
        self,
        video_path: str,
        *,
        task: Optional[str] = None,
        instruction: Optional[str] = None,
        text: str = "",
    ) -> list[float]:
        final_instruction = self._resolve_instruction(task=task, instruction=instruction)
        messages = self._build_video_messages(
            video_path=video_path,
            instruction=final_instruction,
            text=text,
        )
        if task == "video_indexing":
            # Batch scheduling owns retries so every round sends one HTTP request.
            return self._post_embeddings(messages, max_attempts=1)
        return self._post_embeddings(messages)

    async def embed_video(
        self,
        video_path: str,
        *,
        task: Optional[str] = None,
        instruction: Optional[str] = None,
        text: str = "",
    ) -> list[float]:
        return await asyncio.to_thread(
            self.embed_video_sync,
            video_path,
            task=task,
            instruction=instruction,
            text=text,
        )

    async def embed_image_text(
        self,
        image_url: str,
        text: str,
        *,
        task: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> list[float]:
        final_instruction = self._resolve_instruction(task=task, instruction=instruction)
        messages = self._build_image_text_messages(
            image_url=str(image_url or "").strip(),
            text=str(text or ""),
            instruction=final_instruction,
        )
        return await asyncio.to_thread(self._post_embeddings, messages)
