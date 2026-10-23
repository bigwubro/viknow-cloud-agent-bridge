"""Real VLM slice description client."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from viknow.core.knowledge.video_indexing.domain import SegmentDescription
from viknow.core.scheduler.domain import Priority


class OpenAICompatibleSegmentDescriber:
    """Describe one video slice through an OpenAI-compatible multimodal model."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 180,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAICompatibleSegmentDescriber:
        model = os.getenv("VIKNOW_PRIVATE_MODEL")
        base_url = os.getenv("VIKNOW_PRIVATE_BASE_URL")
        api_key = os.getenv("VIKNOW_PRIVATE_API_KEY")
        missing = [
            name
            for name, value in {
                "VIKNOW_PRIVATE_MODEL": model,
                "VIKNOW_PRIVATE_BASE_URL": base_url,
                "VIKNOW_PRIVATE_API_KEY": api_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("real VLM environment is missing: " + ", ".join(missing))
        return cls(model=model or "", base_url=base_url or "", api_key=api_key or "")

    def describe(
        self,
        *,
        slice_id: str,
        clip_path: Path,
        start_seconds: float,
        end_seconds: float,
        sop_context: str = "",
    ) -> SegmentDescription:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": video_data_url(clip_path),
                                "detail": "high",
                                "max_frames": 16,
                                "fps": 1,
                            },
                        },
                        {
                            "type": "text",
                            "text": description_prompt(
                                slice_id=slice_id,
                                start_seconds=start_seconds,
                                end_seconds=end_seconds,
                                sop_context=sop_context,
                            ),
                        },
                    ],
                }
            ],
            "temperature": 0,
            "priority": Priority.BACKGROUND.vllm_priority,
        }
        response = self._chat(payload)
        content = extract_chat_content(response)
        data = parse_json_object(content)
        data["slice_id"] = slice_id
        data = normalize_description_payload(data)
        return SegmentDescription.model_validate(data)

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"segment description request failed: {exc.code}. {detail}") from exc


def description_prompt(
    *,
    slice_id: str,
    start_seconds: float,
    end_seconds: float,
    sop_context: str,
) -> str:
    return (
        f"你正在为视频知识库抽取索引。当前片段是原视频 {start_seconds:.1f}-"
        f"{end_seconds:.1f} 秒，片段 ID 是 {slice_id}。\n"
        f"业务背景：{sop_context or '无 SOP 背景，请仅根据画面描述。'}\n"
        "只输出 JSON 对象，不要输出 Markdown。字段必须是："
        "title, action, semantic, objects, states, state_changes, evidence_notes, confidence。"
        "title/action/semantic/evidence_notes 使用中文字符串；objects/states/state_changes "
        "是中文字符串数组；confidence 是 0 到 1 的数字。如果画面证据不足，请降低 "
        "confidence 并在 evidence_notes 说明。"
    )


def extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"chat response missing choices: {payload}")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"chat response missing message: {payload}")
    content = message.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def parse_json_object(value: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", value, flags=re.S)
    if not match:
        raise RuntimeError(f"VLM response does not contain a JSON object: {value}")
    return json.loads(match.group(0))


def normalize_description_payload(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for key in ["title", "action", "semantic", "evidence_notes"]:
        normalized[key] = coerce_text(normalized.get(key, ""))
    for key in ["objects", "states", "state_changes"]:
        normalized[key] = coerce_text_list(normalized.get(key, []))
    normalized["confidence"] = float(normalized.get("confidence", 0.0) or 0.0)
    return normalized


def coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    return str(value)


def coerce_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def video_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:video/mp4;base64,{encoded}"
