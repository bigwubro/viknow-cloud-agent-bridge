from __future__ import annotations

import asyncio
import base64
import json
import math
import mimetypes
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class RerankMode(StrEnum):
    TEXT = "text"
    MULTIMODAL = "multimodal"


MediaKind = Literal["image", "video"]


@dataclass(frozen=True)
class RerankMedia:
    kind: MediaKind
    path: str | Path | None = None
    data_url: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class RerankCandidate:
    id: str
    text: str = ""
    media: Sequence[RerankMedia] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RerankResult:
    candidate_id: str
    index: int
    relevance_score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class RerankerBatchError(RuntimeError):
    """Raised after a reranker batch exhausts all retry attempts."""


class OpenAICompatibleReranker:
    """OpenAI-compatible text and multimodal reranker client."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        api_url: str,
        mode: RerankMode | str = RerankMode.TEXT,
        batch_size: int = 4,
        batch_concurrency: int = 4,
        max_inline_media_mb: float = 48.0,
        max_retries: int = 2,
        retry_delay_seconds: float = 2.0,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.mode = mode if isinstance(mode, RerankMode) else RerankMode(mode)
        self.batch_size = max(1, int(batch_size))
        self.batch_concurrency = max(1, int(batch_concurrency))
        self.max_inline_media_bytes = max(1, int(float(max_inline_media_mb) * 1024 * 1024))
        self.max_retries = max(0, int(max_retries))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> list[RerankResult]:
        if not self.api_key:
            raise RuntimeError("reranker api_key is required")
        if not candidates:
            return []

        documents = [self._candidate_to_document(candidate) for candidate in candidates]
        batches = self._split_batches(documents)
        semaphore = asyncio.Semaphore(self.batch_concurrency)

        async def process_batch(offset: int, batch_documents: list[Any]) -> list[dict[str, Any]]:
            async with semaphore:
                return await self._call_batch(query, batch_documents, offset)

        batch_result_groups = await asyncio.gather(
            *(process_batch(offset, batch_documents) for offset, batch_documents in batches)
        )

        results: list[RerankResult] = []
        for batch_results in batch_result_groups:
            for item in batch_results:
                index = int(item.get("index", 0))
                if index < 0 or index >= len(candidates):
                    continue
                metadata = dict(item)
                metadata.pop("document", None)
                score = item.get("relevance_score", item.get("score"))
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise RuntimeError(
                        "reranker response missing a finite numeric relevance score"
                    )
                results.append(
                    RerankResult(
                        candidate_id=candidates[index].id,
                        index=index,
                        relevance_score=float(score),
                        metadata=metadata,
                    )
                )
        return results

    def _candidate_to_document(self, candidate: RerankCandidate) -> Any:
        text = str(candidate.text or "").strip()
        if self.mode == RerankMode.TEXT:
            return text or "None"

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for media in candidate.media:
            data_url = self._media_to_data_url(media)
            if media.kind == "video":
                content.append({"type": "video_url", "video_url": {"url": data_url}})
            elif media.kind == "image":
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            else:
                raise ValueError(f"unsupported rerank media kind: {media.kind}")
        return {"content": content} if content else text or "None"

    def _media_to_data_url(self, media: RerankMedia) -> str:
        if media.data_url:
            if media.data_url.startswith(("http://", "https://")):
                return media.data_url
            if media.data_url.startswith("data:"):
                return media.data_url
            mime_type = media.mime_type or self._default_mime_type(media.kind)
            return f"data:{mime_type};base64,{media.data_url}"
        if media.path is None:
            raise ValueError("rerank media requires either data_url or path")
        path = Path(media.path)
        if not path.is_file():
            raise FileNotFoundError(f"rerank media file not found: {path}")
        mime_type = media.mime_type or mimetypes.guess_type(str(path))[0]
        mime_type = mime_type or self._default_mime_type(media.kind)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _default_mime_type(kind: MediaKind) -> str:
        if kind == "video":
            return "video/mp4"
        if kind == "image":
            return "image/jpeg"
        raise ValueError(f"unsupported rerank media kind: {kind}")

    def _split_batches(self, documents: Sequence[Any]) -> list[tuple[int, list[Any]]]:
        batches: list[tuple[int, list[Any]]] = []
        current_documents: list[Any] = []
        current_media_bytes = 0
        current_offset = 0

        for document_index, document in enumerate(documents):
            document_media_bytes = self._inline_media_size(document)
            exceeds_count = len(current_documents) >= self.batch_size
            exceeds_media = (
                bool(current_documents)
                and current_media_bytes + document_media_bytes > self.max_inline_media_bytes
            )
            if exceeds_count or exceeds_media:
                batches.append((current_offset, current_documents))
                current_documents = []
                current_media_bytes = 0
                current_offset = document_index

            current_documents.append(document)
            current_media_bytes += document_media_bytes

        if current_documents:
            batches.append((current_offset, current_documents))
        return batches

    @staticmethod
    def _inline_media_size(document: Any) -> int:
        if not isinstance(document, dict):
            return 0
        total = 0
        for part in document.get("content") or []:
            if part.get("type") == "video_url":
                total += len(str((part.get("video_url") or {}).get("url") or ""))
            elif part.get("type") == "image_url":
                total += len(str((part.get("image_url") or {}).get("url") or ""))
        return total

    async def _call_batch(
        self,
        query: str,
        documents: list[Any],
        offset: int,
    ) -> list[dict[str, Any]]:
        max_attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response_payload = await asyncio.to_thread(
                    self._post_rerank_payload,
                    {
                        "model": self.model,
                        "query": query,
                        "documents": documents,
                    },
                )
                return self._adjust_batch_results(response_payload, offset)
            except (OSError, RuntimeError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                delay = self.retry_delay_seconds * (2 ** (attempt - 1))
                if delay > 0:
                    await asyncio.sleep(delay)
        raise RerankerBatchError(
            f"reranker batch failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    def _post_rerank_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.api_url,
            data=body,
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"reranker request failed: {exc.code}. {detail}") from exc
        try:
            from hyperrag.observability.index_usage import (
                record_generation,
                usage_from_openai_response,
            )

            record_generation(
                name="rerank",
                model=self.model,
                usage=usage_from_openai_response(response_payload),
            )
        except Exception:
            pass
        return response_payload

    @staticmethod
    def _adjust_batch_results(
        response_payload: dict[str, Any],
        offset: int,
    ) -> list[dict[str, Any]]:
        raw_results = response_payload.get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError(f"reranker response missing results: {response_payload}")
        adjusted_results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            adjusted = dict(item)
            adjusted["index"] = int(item.get("index", 0)) + offset
            adjusted.pop("document", None)
            adjusted_results.append(adjusted)
        return adjusted_results
