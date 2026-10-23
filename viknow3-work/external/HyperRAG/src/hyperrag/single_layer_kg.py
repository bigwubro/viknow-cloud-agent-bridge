from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI

from .chunker import HyperChunker
from .config_loader import (
    HyperRAGConfig,
    load_config_from_yaml,
)
from .kg.constants import (
    PRECOMPILED_CHUNK_ID,
    PRECOMPILED_FILE_PATH,
    PRECOMPILED_LIBRARY_ID,
)
from .kg.neo4j_driver import create_async_neo4j_driver, run_with_neo4j_retry
from .kg.package import (
    build_precompile_export_package,
)
from .kg.precompile_graph import build_precompiled_import_payload
from .kg.repository_cleanup import (
    clear_workspace as clear_kg_workspace,
)
from .kg.repository_cleanup import (
    clear_workspaces_by_prefix as clear_kg_workspaces_by_prefix,
)
from .kg.repository_export import export_graph_records as export_kg_repository_records
from .kg.repository_read import (
    show_document_graph as show_kg_document_graph,
)
from .kg.repository_read import (
    show_graph_summary as show_kg_graph_summary,
)
from .kg.repository_read import (
    show_synonyms as show_kg_synonyms,
)
from .kg_merge_summary import EntityMergeSummaryHelper
from .kg_vector_store import KGEmbeddingClient, KGVectorStore
from .model_func import VLLM_PRIORITY_AGENT, extra_body_with_vllm_priority
from .parser import MinerUParser
from .runtime_primitives import SimpleTokenizer, initialize_share_data
from .schema import Asset, Segment
from .video_media import (
    build_video_embedding_text,
    encode_video_segments,
    resolve_segment_video_path,
)

logger = logging.getLogger(__name__)

try:
    import tiktoken
except Exception:  # pragma: no cover
    tiktoken = None


DEFAULT_ENTITY_TYPES = [
    "Person",
    "Organization",
    "NaturalObject",
    "Location",
    "Event",
    "Concept",
    "Method",
    "Process",
    "Step",
    "Component",
    "Collection",
    "Condition",
]

SINGLE_LAYER_ENTITY_LABEL = "TLKGEntity"


def index_storage_path(file_path: str) -> str:
    """图谱和向量行使用的 file_path 键。

    绝对路径仍做 Path.resolve()。相对 Gateway 键保持原样，避免被解析到进程工作目录后和库里的原字符串对不上。
    """
    text = str(file_path or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    return str(path)


SINGLE_LAYER_RELATION_TYPE = "TLKG_RELATION"
LINT_ISSUE_LABEL = "TLKGLintIssue"
FAST_INGEST_POST_COMPILE_FILE_PATH = "fast_ingest://post_compile"
FAST_INGEST_POST_COMPILE_SOURCE = "FAST_INGEST_POST_COMPILE"
COMPACT_ER_MAX_ENTITIES = 0
COMPACT_ER_MAX_RELATIONS = 6
COMPACT_ER_MAX_SPLIT_DEPTH = 3
COMPACT_ER_MIN_SPLIT_CHARS = 160
COMPACT_ER_SPLIT_OVERLAP_CHARS = 80


def _emit_progress(message: str) -> None:
    logger.info(message)
    print(message, flush=True)


def _now_ts() -> int:
    return int(time.time())


_TRACE_TEXT_LIMIT = 16000


def _trace_text(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|password|secret)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text[:_TRACE_TEXT_LIMIT] + ("... [truncated]" if len(text) > _TRACE_TEXT_LIMIT else "")


def _classify_text_generation(prompt: str) -> tuple[str, str]:
    if "\u6587\u6863\u5185\u5bb9" in prompt and "\u7b80\u77ed\u7684\u6458\u8981" in prompt:
        return "hyperrag_document_summary", "document_summary"
    if "\u751f\u6210\u8fd9\u4e2a chunk \u7684\u6807\u9898" in prompt:
        return "hyperrag_chunk_title", "chunk_title"
    if "\u4e0a\u4e0b\u6587\u8bf4\u660e" in prompt and "<chunk>" in prompt:
        return "hyperrag_chunk_context", "chunk_context"
    return "kg_llm", "general"


def _observe_model_generation(
    *,
    name: str,
    model: str,
    messages: list[dict[str, str]],
    response: Any | None = None,
    output: str | None = None,
    metadata: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Record every actual model request without coupling HyperRAG to ViKnow."""
    try:
        from hyperrag.observability.index_usage import (
            record_generation,
            usage_from_openai_response,
        )

        record_generation(
            name=name,
            model=model,
            usage=usage_from_openai_response(response) if response is not None else None,
            metadata=metadata,
            input=[
                {"role": item["role"], "content": _trace_text(item["content"])} for item in messages
            ],
            output=_trace_text(output),
            error=(f"{type(error).__name__}: {_trace_text(error)}" if error is not None else None),
        )
    except Exception:
        logger.warning("model generation observer failed", exc_info=True)


def _md5_id(prefix: str, *parts: str) -> str:
    digest = hashlib.md5("::".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _emit_progress(message: str) -> None:
    logger.info(message)
    print(message, flush=True)


def _normalize_key(value: str) -> str:
    normalized = _normalize_text(value).lower()
    if not normalized:
        return ""
    normalized = re.sub(r"[®™©]", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_entity_lookup_key(value: str) -> str:
    ascii_key = _normalize_key(value)
    if ascii_key:
        return ascii_key
    return _normalize_text(value).lower()


def _truncate_text(value: str, limit: int) -> str:
    normalized = _normalize_text(value)
    if limit <= 0 or len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip()


def _compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def _extract_chunk_text(raw_content: str) -> str:
    try:
        parsed = json.loads(raw_content)
        if isinstance(parsed, dict):
            chunk_text = str(parsed.get("content") or "")
            context = parsed.get("context")
            if isinstance(context, str) and context.strip():
                return f'### context\n\n"{context}"\n\n### chunk text\n\n"{chunk_text}"'
            return chunk_text
    except Exception:
        pass
    return raw_content


def _merge_unique_text(left: str, right: str) -> str:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm:
        return right_norm
    if not right_norm:
        return left_norm
    if left_norm == right_norm:
        return left_norm
    return f"{left_norm}\n{right_norm}"


def _dedupe_keep_order(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_text(str(value))
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(normalized)
    return out


@lru_cache(maxsize=16)
def _build_chunk_tokenizer(model_name: str):
    tokenizer = SimpleTokenizer(model_name=model_name)
    if getattr(tokenizer, "used_fallback", False):
        logger.warning(
            "Unsupported tiktoken model '%s', falling back to '%s' for chunking",
            model_name,
            getattr(tokenizer, "resolved_model_name", "gpt-4o-mini"),
        )
    return tokenizer


_SEARCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./+-]+|[\u4e00-\u9fff]+")


def _normalize_search_text(value: str) -> str:
    return _normalize_text(value).lower()


def _tokenize_search_text(value: str) -> list[str]:
    tokens: list[str] = []
    for match in _SEARCH_TOKEN_PATTERN.findall(_normalize_search_text(value)):
        token = match.strip()
        if not token:
            continue
        tokens.append(token)
        condensed = re.sub(r"[^a-z0-9]+", "", token)
        if condensed and condensed != token:
            tokens.append(condensed)
    return _dedupe_keep_order(tokens)


def _phrase_score(haystack: str, needle: str, weight: float) -> float:
    haystack_norm = _normalize_search_text(haystack)
    needle_norm = _normalize_search_text(needle)
    if not haystack_norm or not needle_norm:
        return 0.0
    if needle_norm in haystack_norm:
        return weight
    return 0.0


def _token_overlap_score(haystack: str, keyword: str, weight: float) -> float:
    haystack_tokens = set(_tokenize_search_text(haystack))
    keyword_tokens = _tokenize_search_text(keyword)
    if not haystack_tokens or not keyword_tokens:
        return 0.0
    matched = sum(1 for token in keyword_tokens if token in haystack_tokens)
    if matched == 0:
        return 0.0
    return weight * (matched / max(1, len(keyword_tokens)))


def _score_keyword_matches(
    haystack: str,
    keywords: list[str],
    *,
    phrase_weight: float,
    token_weight: float,
) -> tuple[float, list[str]]:
    score = 0.0
    matched_terms: list[str] = []
    for keyword in _dedupe_keep_order(keywords):
        term_score = _phrase_score(haystack, keyword, phrase_weight)
        if term_score <= 0:
            term_score = _token_overlap_score(haystack, keyword, token_weight)
        if term_score <= 0:
            continue
        score += term_score
        matched_terms.append(keyword)
    return score, matched_terms


def _derive_relation_keywords(
    source_entity: str,
    target_entity: str,
    relationship_description: str,
    situation: str,
    raw_keywords: Iterable[str] | None = None,
    *,
    limit: int = 6,
) -> list[str]:
    candidates: list[str] = []
    if raw_keywords:
        candidates.extend(str(item) for item in raw_keywords if str(item).strip())
    candidates.extend([source_entity, target_entity])
    for text in (relationship_description, situation):
        for token in _tokenize_search_text(text):
            if len(token) <= 1:
                continue
            candidates.append(token)
    return _dedupe_keep_order(candidates)[:limit]


@dataclass
class ChunkRecord:
    chunk_id: str
    file_path: str
    order: int
    raw_content: str
    text: str
    tokens: int
    segment_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _build_video_segment_chunk_ids(
    modality: str,
    chunks: list[ChunkRecord],
) -> dict[str, str]:
    if str(modality or "").lower() != "video":
        return {}

    segment_chunk_ids: dict[str, str] = {}
    for chunk in chunks:
        chunk_id = str(chunk.chunk_id or "").strip()
        if not chunk_id:
            continue
        for segment_id in chunk.segment_ids:
            normalized_segment_id = str(segment_id or "").strip()
            if normalized_segment_id:
                segment_chunk_ids.setdefault(normalized_segment_id, chunk_id)
    return segment_chunk_ids


@dataclass
class ExtractedEntity:
    extraction_id: str
    file_path: str
    chunk_id: str
    entity_name: str
    entity_type: str
    entity_description: str
    aliases: list[str] = field(default_factory=list)
    normalized_name: str = ""

    @property
    def entity_key(self) -> str:
        return (
            f"{_normalize_key(self.entity_type)}::"
            f"{self.normalized_name or _normalize_entity_lookup_key(self.entity_name)}"
        )


@dataclass
class ExtractedRelation:
    extraction_id: str
    file_path: str
    chunk_id: str
    source_entity: str
    target_entity: str
    relationship_description: str
    situation: str = ""
    relation_keywords: list[str] = field(default_factory=list)


@dataclass
class ExtractionParseResult:
    payload: dict[str, Any]
    parse_mode: str
    raw_content: str
    parse_success: bool
    repair_used: bool = False
    recovery_used: bool = False


@dataclass
class ExtractionAttempt:
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    finish_reason: str
    parse_result: ExtractionParseResult
    cap_violation: bool = False

    @property
    def truncated(self) -> bool:
        if self.finish_reason == "length":
            return True
        if not self.parse_result.raw_content:
            return False
        return self.parse_result.repair_used or not self.parse_result.parse_success


@dataclass
class GraphEntityNode:
    node_id: str
    entity_name: str
    entity_type: str
    entity_description: str
    file_path: str
    status: str = ""
    library_id: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    normalized_name: str = ""
    alias_keys: list[str] = field(default_factory=list)
    extraction_ids: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    created_at: int = field(default_factory=_now_ts)


@dataclass
class GraphRelationEdge:
    relation_id: str
    source_entity: str
    target_entity: str
    relationship_description: str
    situation: str
    relation_keywords: list[str]
    synonym: bool
    file_path: str
    status: str = ""
    library_id: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    created_at: int = field(default_factory=_now_ts)


@dataclass
class QueryKeywords:
    query: str
    entity_keywords: list[str] = field(default_factory=list)
    topic_keywords: list[str] = field(default_factory=list)
    fallback_keywords: list[str] = field(default_factory=list)

    def all_keywords(self) -> list[str]:
        return _dedupe_keep_order(
            self.entity_keywords + self.topic_keywords + self.fallback_keywords
        )


@dataclass
class QueryEntityHit:
    item_id: str
    entity_name: str
    entity_type: str
    entity_description: str
    file_path: str
    aliases: list[str]
    chunk_ids: list[str]
    score: float
    vector_score: float = 0.0
    normalized_name: str = ""
    synonym_of: str | None = None
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class QueryRelationHit:
    relation_id: str
    source_entity: str
    target_entity: str
    relationship_description: str
    situation: str
    file_path: str
    chunk_ids: list[str]
    score: float
    vector_score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    relation_keywords: list[str] = field(default_factory=list)


@dataclass
class QueryChunkHit:
    chunk_id: str
    file_path: str
    order: int
    text: str
    score: float
    vector_score: float = 0.0
    entity_hits: list[str] = field(default_factory=list)
    relation_hits: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResult:
    workspace: str
    file_path: str
    query: str
    artifact_dir: str
    query_scope: str
    keywords: QueryKeywords
    entities: list[QueryEntityHit]
    relations: list[QueryRelationHit]
    chunks: list[QueryChunkHit]
    context_text: str
    retrieval_source: str
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "file_path": self.file_path,
            "query": self.query,
            "artifact_dir": self.artifact_dir,
            "query_scope": self.query_scope,
            "retrieval_source": self.retrieval_source,
            "keywords": asdict(self.keywords),
            "entities": [asdict(item) for item in self.entities],
            "relations": [asdict(item) for item in self.relations],
            "chunks": [asdict(item) for item in self.chunks],
            "context_text": self.context_text,
            "stats": self.stats,
        }


@dataclass
class DocumentGraph:
    workspace: str
    file_path: str
    source_uri: str
    modality: str
    title: str
    asset: dict[str, Any]
    segments: list[dict[str, Any]]
    chunks: list[ChunkRecord]
    extracted_entities: list[ExtractedEntity]
    extracted_relations: list[ExtractedRelation]
    entity_nodes: list[GraphEntityNode]
    relation_edges: list[GraphRelationEdge]
    created_at: int = field(default_factory=_now_ts)

    def to_summary(self) -> dict[str, Any]:
        synonym_edges = sum(1 for edge in self.relation_edges if edge.synonym)
        ordinary_edges = len(self.relation_edges) - synonym_edges
        return {
            "workspace": self.workspace,
            "file_path": self.file_path,
            "source_uri": self.source_uri,
            "modality": self.modality,
            "title": self.title,
            "segments": len(self.segments),
            "chunks": len(self.chunks),
            "extracted_entities": len(self.extracted_entities),
            "extracted_relations": len(self.extracted_relations),
            "entity_nodes": len(self.entity_nodes),
            "ordinary_relations": ordinary_edges,
            "synonym_relations": synonym_edges,
            "created_at": self.created_at,
        }


class SingleLayerKGExtractor:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout: int = 120,
        max_concurrent: int = 8,
        entity_types: list[str] | None = None,
        prompt_mode: str = "full",
        max_output_tokens: int = 192,
        entity_description_max_chars: int = 64,
        retry_full_on_empty: bool = False,
        strict_required_fields: bool = False,
        required_field_retry_attempts: int = 1,
        allow_entity_description_fallback: bool = True,
        allow_relation_entity_inference: bool = True,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.entity_types = entity_types or list(DEFAULT_ENTITY_TYPES)
        self.prompt_mode = str(prompt_mode or "full").strip().lower()
        if self.prompt_mode not in {"full", "compact"}:
            self.prompt_mode = "full"
        self.max_output_tokens = max(32, int(max_output_tokens or 192))
        self.entity_description_max_chars = max(16, int(entity_description_max_chars or 64))
        self.retry_full_on_empty = bool(retry_full_on_empty)
        self.strict_required_fields = bool(strict_required_fields)
        self.required_field_retry_attempts = max(1, int(required_field_retry_attempts or 1))
        self.allow_entity_description_fallback = bool(allow_entity_description_fallback)
        self.allow_relation_entity_inference = bool(allow_relation_entity_inference)
        self._client: AsyncOpenAI | None = None
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._diagnostics: dict[str, Any] = {
            "model_calls": 0,
            "finish_reason_distribution": {},
            "truncated_response_count": 0,
            "json_repair_count": 0,
            "text_recovery_count": 0,
            "schema_cap_violation_count": 0,
            "split_retry_chunk_count": 0,
            "split_retry_model_call_count": 0,
            "recovered_relation_count": 0,
        }

    def _client_or_create(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def complete_text(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 1200,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        generation_name, generation_phase = _classify_text_generation(prompt)
        try:
            async with self._semaphore:
                response = await self._client_or_create().chat.completions.create(
                    model=self.model,
                    temperature=0,
                    max_tokens=max_tokens,
                    messages=messages,
                    extra_body=extra_body_with_vllm_priority(),
                )
            output = _normalize_text(response.choices[0].message.content or "")
        except Exception as exc:
            _observe_model_generation(
                name=generation_name,
                model=self.model,
                messages=messages,
                metadata={"phase": generation_phase},
                error=exc,
            )
            raise
        _observe_model_generation(
            name=generation_name,
            model=self.model,
            messages=messages,
            response=response,
            output=output,
            metadata={"phase": generation_phase},
        )
        return output

    async def extract_chunk(
        self,
        *,
        chunk: ChunkRecord,
        modality: str,
        title: str,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
        if self.prompt_mode == "full" and self.strict_required_fields:
            attempts = max(1, self.required_field_retry_attempts)
            for attempt_index in range(attempts):
                attempt = await self._extract_once(
                    chunk=chunk,
                    modality=modality,
                    title=title,
                    prompt_mode="full",
                    max_output_tokens=self.max_output_tokens,
                )
                if not self._has_required_field_gaps(attempt.parse_result.payload):
                    return attempt.entities, attempt.relations
                logger.warning(
                    "Full ER required-field gap detected for chunk=%s attempt=%s/%s; retrying extraction",
                    chunk.chunk_id,
                    attempt_index + 1,
                    attempts,
                )
            raise RuntimeError(
                f"full ER extraction failed required-field checks after {attempts} attempts "
                f"for chunk {chunk.chunk_id}"
            )

        entities, relations = await self._extract_with_split_retry(
            chunk=chunk,
            modality=modality,
            title=title,
            depth=0,
        )
        if (
            self.prompt_mode == "compact"
            and self.retry_full_on_empty
            and not entities
            and not relations
        ):
            attempt = await self._extract_once(
                chunk=chunk,
                modality=modality,
                title=title,
                prompt_mode="full",
                max_output_tokens=max(256, self.max_output_tokens),
            )
            entities, relations = attempt.entities, attempt.relations
        return entities, relations

    @staticmethod
    def _has_required_field_gaps(payload: dict[str, Any]) -> bool:
        entities = payload.get("entities") if isinstance(payload, dict) else []
        if not isinstance(entities, list):
            return False
        for item in entities:
            if not isinstance(item, dict):
                continue
            entity_name = _normalize_text(str(item.get("entity_name") or item.get("name") or ""))
            if not entity_name:
                continue
            entity_description = _normalize_text(
                str(item.get("entity_description") or item.get("description") or "")
            )
            if not entity_description:
                return True
        return False

    def diagnostics_snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._diagnostics))

    async def _extract_with_split_retry(
        self,
        *,
        chunk: ChunkRecord,
        modality: str,
        title: str,
        depth: int,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
        attempt = await self._extract_once(
            chunk=chunk,
            modality=modality,
            title=title,
            prompt_mode=self.prompt_mode,
            max_output_tokens=self.max_output_tokens,
        )
        if self.prompt_mode != "compact" or not attempt.truncated:
            return attempt.entities, attempt.relations

        if depth >= COMPACT_ER_MAX_SPLIT_DEPTH:
            raise RuntimeError(
                f"compact ER output remained truncated after {depth} split retries "
                f"for chunk {chunk.chunk_id}"
            )
        child_chunks = self._split_chunk_for_retry(chunk)
        if child_chunks is None:
            raise RuntimeError(
                f"compact ER output was truncated but chunk {chunk.chunk_id} "
                "is too short to split safely"
            )

        self._diagnostics["split_retry_chunk_count"] += 1
        self._diagnostics["split_retry_model_call_count"] += len(child_chunks)
        child_results = await asyncio.gather(
            *[
                self._extract_with_split_retry(
                    chunk=child,
                    modality=modality,
                    title=title,
                    depth=depth + 1,
                )
                for child in child_chunks
            ]
        )
        entities, relations = self._merge_extraction_results(child_results)
        if depth == 0:
            self._diagnostics["recovered_relation_count"] += len(relations)
        return entities, relations

    async def _extract_once(
        self,
        *,
        chunk: ChunkRecord,
        modality: str,
        title: str,
        prompt_mode: str,
        max_output_tokens: int,
    ) -> ExtractionAttempt:
        prompt = self._build_prompt(
            chunk=chunk,
            modality=modality,
            title=title,
            prompt_mode=prompt_mode,
        )
        system_prompt = self._build_system_prompt(prompt_mode=prompt_mode)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        try:
            async with self._semaphore:
                response = await self._client_or_create().chat.completions.create(
                    model=self.model,
                    temperature=0,
                    max_tokens=max(32, int(max_output_tokens)),
                    response_format=self._response_format(prompt_mode=prompt_mode),
                    messages=messages,
                    extra_body=extra_body_with_vllm_priority(),
                )
            output = response.choices[0].message.content or ""
        except Exception as exc:
            _observe_model_generation(
                name="kg_er_extract",
                model=self.model,
                messages=messages,
                metadata={"prompt_mode": prompt_mode},
                error=exc,
            )
            raise
        _observe_model_generation(
            name="kg_er_extract",
            model=self.model,
            messages=messages,
            response=response,
            output=output,
            metadata={"prompt_mode": prompt_mode},
        )
        choice = response.choices[0]
        finish_reason = str(choice.finish_reason or "")
        parse_result = self._parse_json_with_diagnostics(output)
        payload = self._normalize_payload_schema(parse_result.payload)
        cap_violation = False
        if prompt_mode == "compact":
            payload, cap_violation = self._enforce_compact_caps(payload)
        entities, relations = self._build_assertions(chunk=chunk, payload=payload)
        self._record_attempt_diagnostics(
            finish_reason=finish_reason,
            parse_result=parse_result,
            cap_violation=cap_violation,
        )
        return ExtractionAttempt(
            entities=entities,
            relations=relations,
            finish_reason=finish_reason,
            parse_result=parse_result,
            cap_violation=cap_violation,
        )

    def _response_format(self, *, prompt_mode: str) -> dict[str, Any]:
        if prompt_mode != "compact":
            return {"type": "json_object"}
        short_description_limit = min(32, self.entity_description_max_chars)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["relations"],
            "properties": {
                "relations": {
                    "type": "array",
                    "maxItems": COMPACT_ER_MAX_RELATIONS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["src", "tgt", "description", "context"],
                        "properties": {
                            "src": {"type": "string", "maxLength": 64},
                            "tgt": {"type": "string", "maxLength": 64},
                            "description": {
                                "type": "string",
                                "maxLength": short_description_limit,
                            },
                            "context": {
                                "type": "string",
                                "maxLength": short_description_limit,
                            },
                        },
                    },
                },
            },
        }
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "compact_er_extraction",
                "strict": True,
                "schema": schema,
            },
        }

    @staticmethod
    def _build_system_prompt(*, prompt_mode: str) -> str:
        if prompt_mode == "compact":
            return (
                "Extract concise entities and relations from one chunk. "
                "Return one valid JSON object only with key relations. "
                "No markdown, no code fence, no prose. "
                "Prefer recall: if the chunk contains explicit actors/actions/causal links, extract them. "
                "Return 1 to 3 highest-signal relations when supported by evidence. "
                "Use 4 to 6 only for dense tables, specifications, or procedures. "
                "Prefer partial extraction over empty output. "
                "Entities are derived from relation endpoints downstream. "
                "For table/spec/SOP/procedure chunks, always extract at least one high-signal relation when evidence exists."
            )
        return (
            "You extract HyperRAG entities and relationships from one chunk. "
            "Return strict JSON only. Keep entity names consistent within the chunk. "
            "Do not output duplicate entities in the same chunk. "
            "For relationships, include a one-sentence relationship_description and a situation field that captures the background or condition, "
            "including critical entity concepts or events such as equipment model, SOP number, version, stage, or operating context when present."
        )

    def _build_prompt(
        self,
        *,
        chunk: ChunkRecord,
        modality: str,
        title: str,
        prompt_mode: str | None = None,
    ) -> str:
        active_mode = prompt_mode or self.prompt_mode
        if active_mode == "compact":
            return json.dumps(
                {
                    "task": "Extract concise entities and relations from one chunk.",
                    "schema": {
                        "relations": [
                            {
                                "src": "str",
                                "tgt": "str",
                                "description": "short str",
                                "context": "short str",
                            }
                        ],
                    },
                    "rules": [
                        "Return JSON only.",
                        "Ground strictly in chunk text.",
                        "Prefer 1 to 3 highest-signal relations when evidence exists.",
                        "Use 4 to 6 only for dense tables, specifications, or procedures.",
                        "Prefer partial extraction over empty output when evidence exists.",
                        "For table/spec/procedure chunks, extract distinct parameter->value or step->action relations.",
                        "Keep output compact: max 6 relations.",
                        "Keep all descriptions short (<= 32 Chinese chars).",
                        'If no result, return {"relations":[]}.',
                    ],
                    "doc": {"title": title, "modality": modality},
                    "chunk": {
                        "id": chunk.chunk_id,
                        "order": chunk.order,
                        "text": chunk.text,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return json.dumps(
            {
                "task": "Extract entities and relationships from a single chunk for a single-layer knowledge graph.",
                "entity_types": self.entity_types,
                "rules": [
                    "Return JSON with top-level keys entities and relations.",
                    "Each entity must contain: entity_name, entity_type, entity_description, aliases.",
                    "Each relation must contain: source_entity, target_entity, relationship_description, situation, relation_keywords.",
                    "Do not repeat the same entity_name in one chunk.",
                    "All output must be grounded in the chunk text.",
                    "The situation field must explain the background for the relation and include key concepts or events when available.",
                    "relation_keywords must be a concise list of 2 to 6 retrieval-oriented technical keywords or phrases.",
                    "Keep entity_description concise and within 300 Chinese characters or equivalent length.",
                ],
                "document": {"title": title, "modality": modality, "file_path": chunk.file_path},
                "chunk": {
                    "chunk_id": chunk.chunk_id,
                    "order": chunk.order,
                    "segment_ids": chunk.segment_ids,
                    "metadata": chunk.metadata,
                    "text": chunk.text,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        return SingleLayerKGExtractor._parse_json_with_diagnostics(content).payload

    @staticmethod
    def _parse_json_with_diagnostics(content: str) -> ExtractionParseResult:
        text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
        if not text:
            return ExtractionParseResult(
                payload={"entities": [], "relations": []},
                parse_mode="empty",
                raw_content="",
                parse_success=True,
            )
        candidates: list[tuple[str, str]] = [("json", text)]

        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if fence_match:
            candidates.append(("fenced_json", fence_match.group(1).strip()))

        brace_match = re.search(r"\{.*\}", text, re.S)
        if brace_match:
            candidates.append(("embedded_json", brace_match.group(0).strip()))
        candidates.extend(
            ("balanced_json", candidate)
            for candidate in SingleLayerKGExtractor._extract_balanced_json_candidates(text)
        )

        repaired_candidates: list[tuple[str, str]] = []
        for _, candidate in candidates:
            repaired = SingleLayerKGExtractor._complete_json_delimiters(candidate)
            if repaired is not None and repaired != candidate:
                repaired_candidates.append(("repaired_json", repaired))

        for parse_mode, candidate in [*candidates, *repaired_candidates]:
            try:
                parsed = json.loads(candidate)
                payload = SingleLayerKGExtractor._coerce_payload(parsed)
                if payload is not None:
                    return ExtractionParseResult(
                        payload=payload,
                        parse_mode=parse_mode,
                        raw_content=text,
                        parse_success=True,
                        repair_used=parse_mode == "repaired_json",
                    )
            except Exception:
                pass
            try:
                parsed = ast.literal_eval(candidate)
                payload = SingleLayerKGExtractor._coerce_payload(parsed)
                if payload is not None:
                    return ExtractionParseResult(
                        payload=payload,
                        parse_mode="python_literal",
                        raw_content=text,
                        parse_success=True,
                        recovery_used=True,
                    )
            except Exception:
                continue

        recovered = SingleLayerKGExtractor._recover_payload_from_text(text)
        if recovered.get("entities") or recovered.get("relations"):
            return ExtractionParseResult(
                payload=recovered,
                parse_mode="text_recovery",
                raw_content=text,
                parse_success=False,
                recovery_used=True,
            )

        logger.warning("Extractor JSON recovery failed")
        return ExtractionParseResult(
            payload={"entities": [], "relations": []},
            parse_mode="failed",
            raw_content=text,
            parse_success=False,
        )

    @staticmethod
    def _enforce_compact_caps(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        entities = list(payload.get("entities") or [])
        relations = list(payload.get("relations") or [])
        violated = (
            len(entities) > COMPACT_ER_MAX_ENTITIES or len(relations) > COMPACT_ER_MAX_RELATIONS
        )
        if violated:
            logger.warning(
                "Compact ER schema cap violation: entities=%s relations=%s",
                len(entities),
                len(relations),
            )
        return {
            "entities": entities[:COMPACT_ER_MAX_ENTITIES],
            "relations": relations[:COMPACT_ER_MAX_RELATIONS],
        }, violated

    def _record_attempt_diagnostics(
        self,
        *,
        finish_reason: str,
        parse_result: ExtractionParseResult,
        cap_violation: bool,
    ) -> None:
        self._diagnostics["model_calls"] += 1
        distribution = self._diagnostics["finish_reason_distribution"]
        reason = finish_reason or "unknown"
        distribution[reason] = int(distribution.get(reason) or 0) + 1
        if finish_reason == "length" or (
            parse_result.raw_content
            and (parse_result.repair_used or not parse_result.parse_success)
        ):
            self._diagnostics["truncated_response_count"] += 1
        if parse_result.repair_used:
            self._diagnostics["json_repair_count"] += 1
        if parse_result.parse_mode == "text_recovery":
            self._diagnostics["text_recovery_count"] += 1
        if cap_violation:
            self._diagnostics["schema_cap_violation_count"] += 1

    @staticmethod
    def _split_chunk_for_retry(
        chunk: ChunkRecord,
    ) -> tuple[ChunkRecord, ChunkRecord] | None:
        text = chunk.text.strip()
        if len(text) < COMPACT_ER_MIN_SPLIT_CHARS * 2:
            return None
        midpoint = len(text) // 2
        boundary_pattern = re.compile(r"\n\n+|\n|[。！？.!?；;]")
        boundaries = [match.end() for match in boundary_pattern.finditer(text)]
        split_at = min(boundaries, key=lambda pos: abs(pos - midpoint)) if boundaries else midpoint
        if (
            split_at < COMPACT_ER_MIN_SPLIT_CHARS
            or len(text) - split_at < COMPACT_ER_MIN_SPLIT_CHARS
        ):
            split_at = midpoint
        overlap = min(COMPACT_ER_SPLIT_OVERLAP_CHARS, split_at, len(text) - split_at)
        left_text = text[: split_at + overlap].strip()
        right_text = text[split_at - overlap :].strip()
        if not left_text or not right_text or left_text == text or right_text == text:
            return None

        def _child(child_text: str, part: int) -> ChunkRecord:
            metadata = dict(chunk.metadata)
            metadata["er_split_part"] = part
            return ChunkRecord(
                chunk_id=chunk.chunk_id,
                file_path=chunk.file_path,
                order=chunk.order,
                raw_content=child_text,
                text=child_text,
                tokens=max(1, chunk.tokens // 2),
                segment_ids=list(chunk.segment_ids),
                metadata=metadata,
            )

        return _child(left_text, 1), _child(right_text, 2)

    @staticmethod
    def _merge_extraction_results(
        results: Iterable[tuple[list[ExtractedEntity], list[ExtractedRelation]]],
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
        entity_map: dict[str, ExtractedEntity] = {}
        relation_map: dict[str, ExtractedRelation] = {}
        for entities, relations in results:
            for entity in entities:
                key = entity.entity_key
                current = entity_map.get(key)
                if current is None:
                    entity_map[key] = entity
                    continue
                current.entity_description = _merge_unique_text(
                    current.entity_description,
                    entity.entity_description,
                )
                current.aliases = _dedupe_keep_order(current.aliases + entity.aliases)
            for relation in relations:
                key = "::".join(
                    [
                        _normalize_entity_lookup_key(relation.source_entity),
                        _normalize_entity_lookup_key(relation.target_entity),
                    ]
                )
                current = relation_map.get(key)
                if current is None:
                    relation_map[key] = relation
                    continue
                current.relationship_description = _merge_unique_text(
                    current.relationship_description,
                    relation.relationship_description,
                )
                current.situation = _merge_unique_text(current.situation, relation.situation)
                current.relation_keywords = _dedupe_keep_order(
                    current.relation_keywords + relation.relation_keywords
                )
        return list(entity_map.values()), list(relation_map.values())

    @staticmethod
    def _recover_payload_from_text(text: str) -> dict[str, Any]:
        relation_blocks: list[dict[str, Any]] = []
        entity_blocks: list[dict[str, Any]] = []
        block_candidates = re.findall(r"\{[^{}]{20,800}\}", text or "", re.S)
        kv_pattern = re.compile(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s*:\s*"([^"\n]{1,160})"')

        for block in block_candidates:
            kvs = {str(k).strip().lower(): str(v).strip() for k, v in kv_pattern.findall(block)}
            if not kvs:
                continue
            src = (
                kvs.get("source_entity")
                or kvs.get("source_name")
                or kvs.get("source")
                or kvs.get("s")
                or ""
            )
            tgt = (
                kvs.get("target_entity")
                or kvs.get("target_name")
                or kvs.get("target")
                or kvs.get("object")
                or kvs.get("o")
                or ""
            )
            rel = (
                kvs.get("relationship_description")
                or kvs.get("relation")
                or kvs.get("predicate")
                or kvs.get("p")
                or kvs.get("description")
                or ""
            )
            situation = kvs.get("situation") or kvs.get("context") or kvs.get("desc") or ""
            if src and tgt and rel:
                relation_blocks.append(
                    {
                        "source_entity": src,
                        "target_entity": tgt,
                        "relationship_description": rel,
                        "situation": situation,
                    }
                )
                continue

            name = kvs.get("entity_name") or kvs.get("name") or kvs.get("entity") or ""
            if name:
                entity_blocks.append(
                    {
                        "entity_name": name,
                        "entity_type": kvs.get("entity_type") or kvs.get("type") or "Concept",
                        "entity_description": kvs.get("entity_description")
                        or kvs.get("description")
                        or kvs.get("desc")
                        or "",
                    }
                )

        return {"entities": entity_blocks, "relations": relation_blocks}

    @staticmethod
    def _coerce_payload(parsed: Any) -> dict[str, Any] | None:
        if isinstance(parsed, dict):
            entities = parsed.get("entities") or parsed.get("ents")
            relations = parsed.get("relations") or parsed.get("triples") or parsed.get("rels")
            if entities is not None or relations is not None:
                return parsed
            # 兼容单条关系/实体的平铺字典
            has_relation_shape = any(
                key in parsed
                for key in (
                    "source_entity",
                    "source_name",
                    "source",
                    "s",
                    "src",
                )
            ) and any(
                key in parsed
                for key in (
                    "target_entity",
                    "target_name",
                    "target",
                    "object",
                    "o",
                    "tgt",
                )
            )
            has_entity_shape = any(key in parsed for key in ("entity_name", "name", "entity"))
            if has_relation_shape:
                return {"entities": [], "relations": [parsed]}
            if has_entity_shape:
                return {"entities": [parsed], "relations": []}
            for key in ("data", "result", "output", "response"):
                nested = parsed.get(key)
                if isinstance(nested, (dict, list)):
                    nested_payload = SingleLayerKGExtractor._coerce_payload(nested)
                    if nested_payload is not None:
                        return nested_payload
            return parsed
        if isinstance(parsed, list):
            if all(isinstance(item, dict) for item in parsed):
                return {"entities": [], "relations": parsed}
        return None

    @staticmethod
    def _complete_json_delimiters(text: str) -> str | None:
        expected_closers: list[str] = []
        in_string = False
        escaped = False
        for char in text:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                expected_closers.append("}")
            elif char == "[":
                expected_closers.append("]")
            elif char in "}]":
                if not expected_closers or expected_closers.pop() != char:
                    return None
        if in_string:
            return None
        return text + "".join(reversed(expected_closers))

    @staticmethod
    def _extract_balanced_json_candidates(text: str, *, limit: int = 4) -> list[str]:
        candidates: list[str] = []
        start = -1
        depth = 0
        in_string = False
        escaped = False
        quote = ""
        for idx, ch in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == quote:
                    in_string = False
                    quote = ""
                continue
            if ch in {'"', "'"}:
                in_string = True
                quote = ch
                continue
            if ch == "{":
                if depth == 0:
                    start = idx
                depth += 1
                continue
            if ch == "}":
                if depth <= 0:
                    continue
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : idx + 1].strip())
                    if len(candidates) >= limit:
                        break
                    start = -1
        return candidates

    def _build_assertions(
        self,
        *,
        chunk: ChunkRecord,
        payload: dict[str, Any],
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
        payload = self._normalize_payload_schema(payload)
        entity_map: dict[str, ExtractedEntity] = {}

        for raw in payload.get("entities", []) or []:
            if not isinstance(raw, dict):
                continue
            entity_name = _normalize_text(str(raw.get("entity_name") or raw.get("name") or ""))
            entity_type = (
                _normalize_text(str(raw.get("entity_type") or raw.get("type") or "Concept"))
                or "Concept"
            )
            entity_description = _truncate_text(
                str(raw.get("entity_description") or raw.get("description") or ""),
                self.entity_description_max_chars,
            )
            if entity_name and not entity_description and self.allow_entity_description_fallback:
                entity_description = _truncate_text(entity_name, self.entity_description_max_chars)
            if not entity_name or not entity_description:
                continue
            normalized_name = _normalize_entity_lookup_key(entity_name)
            entity_key = f"{_normalize_key(entity_type)}::{normalized_name}"
            aliases = _dedupe_keep_order(raw.get("aliases") or [])
            if entity_key in entity_map:
                current = entity_map[entity_key]
                current.entity_description = _merge_unique_text(
                    current.entity_description, entity_description
                )
                current.aliases = _dedupe_keep_order(current.aliases + aliases)
            else:
                extraction_id = _md5_id("entx-", chunk.file_path, chunk.chunk_id, entity_key)
                entity_map[entity_key] = ExtractedEntity(
                    extraction_id=extraction_id,
                    file_path=chunk.file_path,
                    chunk_id=chunk.chunk_id,
                    entity_name=entity_name,
                    entity_type=entity_type,
                    entity_description=entity_description,
                    aliases=aliases,
                    normalized_name=normalized_name,
                )

        relation_map: dict[str, ExtractedRelation] = {}
        for raw in payload.get("relations", []) or []:
            if not isinstance(raw, dict):
                continue
            source_entity = _normalize_text(
                str(raw.get("source_entity") or raw.get("source_name") or "")
            )
            target_entity = _normalize_text(
                str(raw.get("target_entity") or raw.get("target_name") or "")
            )
            relationship_description = _normalize_text(
                str(raw.get("relationship_description") or raw.get("description") or "")
            )
            situation = _normalize_text(str(raw.get("situation") or ""))
            relation_keywords = _derive_relation_keywords(
                source_entity,
                target_entity,
                relationship_description,
                situation,
                raw.get("relation_keywords") or raw.get("keywords") or [],
            )
            if not source_entity or not target_entity or not relationship_description:
                continue
            relation_key = "::".join(
                [
                    _normalize_entity_lookup_key(source_entity),
                    _normalize_entity_lookup_key(target_entity),
                ]
            )
            if relation_key in relation_map:
                current = relation_map[relation_key]
                current.relationship_description = _merge_unique_text(
                    current.relationship_description,
                    relationship_description,
                )
                current.situation = _merge_unique_text(current.situation, situation)
                current.relation_keywords = _dedupe_keep_order(
                    current.relation_keywords + relation_keywords
                )
            else:
                extraction_id = _md5_id("relx-", chunk.file_path, chunk.chunk_id, relation_key)
                relation_map[relation_key] = ExtractedRelation(
                    extraction_id=extraction_id,
                    file_path=chunk.file_path,
                    chunk_id=chunk.chunk_id,
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relationship_description=relationship_description,
                    situation=situation,
                    relation_keywords=relation_keywords,
                )

        if relation_map and self.allow_relation_entity_inference:
            known_names = {
                _normalize_entity_lookup_key(entity.entity_name): entity
                for entity in entity_map.values()
            }
            for relation in relation_map.values():
                for entity_name in (relation.source_entity, relation.target_entity):
                    normalized_name = _normalize_entity_lookup_key(entity_name)
                    if normalized_name in known_names:
                        continue
                    entity_key = f"concept::{normalized_name}"
                    extraction_id = _md5_id(
                        "entx-auto-", chunk.file_path, chunk.chunk_id, entity_key
                    )
                    inferred = ExtractedEntity(
                        extraction_id=extraction_id,
                        file_path=chunk.file_path,
                        chunk_id=chunk.chunk_id,
                        entity_name=entity_name,
                        entity_type="Concept",
                        entity_description=_truncate_text(
                            entity_name, self.entity_description_max_chars
                        ),
                        aliases=[],
                        normalized_name=normalized_name,
                    )
                    entity_map[entity_key] = inferred
                    known_names[normalized_name] = inferred

        return list(entity_map.values()), list(relation_map.values())

    @staticmethod
    def _normalize_payload_schema(payload: dict[str, Any]) -> dict[str, Any]:
        entities_raw = payload.get("entities") or payload.get("ents") or []
        relations_raw = (
            payload.get("relations") or payload.get("triples") or payload.get("rels") or []
        )

        entities: list[dict[str, Any]] = []
        for item in entities_raw:
            if not isinstance(item, dict):
                continue
            entities.append(
                {
                    "entity_name": item.get("entity_name")
                    or item.get("name")
                    or item.get("entity"),
                    "entity_type": item.get("entity_type") or item.get("type") or "Concept",
                    "entity_description": item.get("entity_description")
                    or item.get("description")
                    or item.get("desc")
                    or "",
                    "aliases": item.get("aliases") or item.get("alias") or [],
                }
            )

        relations: list[dict[str, Any]] = []
        for item in relations_raw:
            if not isinstance(item, dict):
                continue
            relation_desc = (
                item.get("relationship_description")
                or item.get("description")
                or item.get("relation")
                or item.get("predicate")
                or item.get("p")
                or ""
            )
            relation_ctx = item.get("situation") or item.get("context") or item.get("desc") or ""
            relations.append(
                {
                    "source_entity": item.get("source_entity")
                    or item.get("source_name")
                    or item.get("source")
                    or item.get("src")
                    or item.get("s")
                    or "",
                    "target_entity": item.get("target_entity")
                    or item.get("target_name")
                    or item.get("target")
                    or item.get("tgt")
                    or item.get("object")
                    or item.get("o")
                    or "",
                    "relationship_description": relation_desc,
                    "situation": relation_ctx,
                    "relation_keywords": item.get("relation_keywords")
                    or item.get("keywords")
                    or [],
                }
            )
        return {"entities": entities, "relations": relations}


class DocScopedSynonymResolver:
    def __init__(
        self,
        *,
        workspace: str,
        merge_summary_helper: EntityMergeSummaryHelper | None = None,
    ) -> None:
        self.workspace = workspace
        self.merge_summary_helper = merge_summary_helper

    async def resolve(
        self,
        *,
        file_path: str,
        entities: list[ExtractedEntity],
        relations: list[ExtractedRelation],
    ) -> tuple[list[GraphEntityNode], list[GraphRelationEdge], dict[str, str], dict[str, Any]]:
        metrics = {
            "exact_name_merges": 0,
            "alias_merges": 0,
            "entity_groups_after_exact": 0,
            "entity_groups_after_alias": 0,
        }

        occurrence_to_node: dict[str, str] = {}
        exact_groups: dict[str, list[ExtractedEntity]] = {}
        for entity in entities:
            exact_groups.setdefault(
                entity.normalized_name or _normalize_entity_lookup_key(entity.entity_name),
                [],
            ).append(entity)

        exact_nodes: list[GraphEntityNode] = []
        for group in exact_groups.values():
            node = await self._build_node_from_entities(group, file_path=file_path)
            exact_nodes.append(node)
            for entity in group:
                occurrence_to_node[entity.extraction_id] = node.entity_name
            metrics["exact_name_merges"] += max(0, len(group) - 1)

        metrics["entity_groups_after_exact"] = len(exact_nodes)

        alias_groups = self._group_nodes_by_aliases(exact_nodes)
        final_nodes: list[GraphEntityNode] = []
        exact_name_to_final: dict[str, str] = {}
        for node_group in alias_groups:
            merged_node = await self._merge_node_group(node_group, file_path=file_path)
            final_nodes.append(merged_node)
            for node in node_group:
                exact_name_to_final[node.entity_name] = merged_node.entity_name
            metrics["alias_merges"] += max(0, len(node_group) - 1)

        metrics["entity_groups_after_alias"] = len(final_nodes)

        for extraction_id, entity_name in list(occurrence_to_node.items()):
            occurrence_to_node[extraction_id] = exact_name_to_final.get(entity_name, entity_name)

        relation_name_to_node: dict[str, str] = {}
        for entity in entities:
            merged_name = occurrence_to_node.get(entity.extraction_id)
            if merged_name is not None:
                relation_name_to_node[
                    entity.normalized_name or _normalize_entity_lookup_key(entity.entity_name)
                ] = merged_name

        relation_edges: dict[str, GraphRelationEdge] = {}
        for relation in relations:
            source_entity = relation_name_to_node.get(
                _normalize_entity_lookup_key(relation.source_entity)
            )
            target_entity = relation_name_to_node.get(
                _normalize_entity_lookup_key(relation.target_entity)
            )
            if source_entity is None or target_entity is None or source_entity == target_entity:
                continue
            relation_key = "::".join(
                [
                    source_entity,
                    target_entity,
                    file_path,
                    "0",
                ]
            )
            edge = relation_edges.get(relation_key)
            if edge is None:
                relation_edges[relation_key] = GraphRelationEdge(
                    relation_id=_md5_id(
                        "rel-",
                        file_path,
                        source_entity,
                        target_entity,
                    ),
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relationship_description=relation.relationship_description,
                    situation=relation.situation,
                    relation_keywords=list(relation.relation_keywords),
                    synonym=False,
                    file_path=file_path,
                    chunk_ids=[relation.chunk_id],
                )
            else:
                edge.relationship_description = _merge_unique_text(
                    edge.relationship_description, relation.relationship_description
                )
                edge.situation = _merge_unique_text(edge.situation, relation.situation)
                edge.relation_keywords = _dedupe_keep_order(
                    edge.relation_keywords + relation.relation_keywords
                )
                edge.chunk_ids = _dedupe_keep_order(edge.chunk_ids + [relation.chunk_id])

        return final_nodes, list(relation_edges.values()), occurrence_to_node, metrics

    async def _build_node_from_entities(
        self,
        entities: list[ExtractedEntity],
        *,
        file_path: str,
    ) -> GraphEntityNode:
        primary = entities[0]
        chunk_ids: list[str] = []
        aliases: list[str] = []
        extraction_ids: list[str] = []
        descriptions: list[str] = []
        for entity in entities:
            descriptions.append(entity.entity_description)
            chunk_ids = _dedupe_keep_order(chunk_ids + [entity.chunk_id])
            aliases = _dedupe_keep_order(aliases + entity.aliases)
            extraction_ids = _dedupe_keep_order(extraction_ids + [entity.extraction_id])
        normalized_name = primary.normalized_name or _normalize_entity_lookup_key(
            primary.entity_name
        )
        aliases = [
            alias for alias in aliases if _normalize_entity_lookup_key(alias) != normalized_name
        ]
        entity_description = await self._merge_entity_descriptions(
            entity_name=primary.entity_name,
            entity_type=primary.entity_type,
            aliases=aliases,
            descriptions=descriptions,
        )
        return GraphEntityNode(
            node_id=_md5_id("ent-", file_path, primary.entity_type, primary.entity_name),
            entity_name=primary.entity_name,
            entity_type=primary.entity_type,
            entity_description=entity_description,
            file_path=file_path,
            chunk_ids=chunk_ids,
            aliases=aliases,
            normalized_name=normalized_name,
            alias_keys=_dedupe_keep_order(
                [_normalize_entity_lookup_key(alias) for alias in aliases]
            ),
            extraction_ids=extraction_ids,
        )

    def _group_nodes_by_aliases(self, nodes: list[GraphEntityNode]) -> list[list[GraphEntityNode]]:
        if not nodes:
            return []

        parent = list(range(len(nodes)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            if left_root < right_root:
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

        alias_index: dict[str, list[int]] = {}
        for index, node in enumerate(nodes):
            match_keys = self._node_match_keys(node)
            matched_indexes: list[int] = []
            for key in match_keys:
                matched_indexes.extend(alias_index.get(key, []))
            for matched_index in matched_indexes:
                union(index, matched_index)
            for key in match_keys:
                alias_index.setdefault(key, []).append(index)

        grouped: dict[int, list[GraphEntityNode]] = {}
        for index, node in enumerate(nodes):
            grouped.setdefault(find(index), []).append(node)
        return list(grouped.values())

    @staticmethod
    def _node_match_keys(node: GraphEntityNode) -> list[str]:
        raw_keys = [_normalize_entity_lookup_key(node.entity_name)]
        raw_keys.extend(_normalize_entity_lookup_key(alias) for alias in node.aliases)
        return [key for key in _dedupe_keep_order(raw_keys) if key]

    async def _merge_node_group(
        self,
        nodes: list[GraphEntityNode],
        *,
        file_path: str,
    ) -> GraphEntityNode:
        primary = nodes[0]
        chunk_ids: list[str] = []
        aliases: list[str] = []
        extraction_ids: list[str] = []
        descriptions: list[str] = []
        for node in nodes:
            descriptions.append(node.entity_description)
            chunk_ids = _dedupe_keep_order(chunk_ids + node.chunk_ids)
            aliases = _dedupe_keep_order(aliases + node.aliases + [node.entity_name])
            extraction_ids = _dedupe_keep_order(extraction_ids + node.extraction_ids)
        normalized_name = primary.normalized_name or _normalize_entity_lookup_key(
            primary.entity_name
        )
        aliases = [
            alias for alias in aliases if _normalize_entity_lookup_key(alias) != normalized_name
        ]
        entity_description = await self._merge_entity_descriptions(
            entity_name=primary.entity_name,
            entity_type=primary.entity_type,
            aliases=aliases,
            descriptions=descriptions,
        )
        return GraphEntityNode(
            node_id=_md5_id("ent-", file_path, primary.entity_type, primary.entity_name),
            entity_name=primary.entity_name,
            entity_type=primary.entity_type,
            entity_description=entity_description,
            file_path=file_path,
            chunk_ids=chunk_ids,
            aliases=aliases,
            normalized_name=normalized_name,
            alias_keys=_dedupe_keep_order(
                [_normalize_entity_lookup_key(alias) for alias in aliases]
            ),
            extraction_ids=extraction_ids,
        )

    async def _merge_entity_descriptions(
        self,
        *,
        entity_name: str,
        entity_type: str,
        aliases: list[str],
        descriptions: list[str],
    ) -> str:
        if self.merge_summary_helper is None:
            merged = ""
            for description in descriptions:
                merged = _merge_unique_text(merged, description)
            return merged
        return await self.merge_summary_helper.merge_descriptions(
            entity_name=entity_name,
            entity_type=entity_type,
            aliases=aliases,
            descriptions=descriptions,
        )


class SingleLayerKGRepository:
    def __init__(
        self,
        *,
        workspace: str,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.uri = uri or os.environ.get("NEO4J_URI") or "neo4j://localhost:17687"
        self.username = username or os.environ.get("NEO4J_USERNAME") or "neo4j"
        self.password = password or os.environ.get("NEO4J_PASSWORD") or "qx123456"
        self.database = database or os.environ.get("NEO4J_DATABASE")
        self.driver = None

    async def initialize(self) -> None:
        self.driver = create_async_neo4j_driver(self.uri, (self.username, self.password))
        async with self.driver.session(database=self.database) as session:
            await session.run(
                f"CREATE CONSTRAINT tlkg_entity_uid IF NOT EXISTS FOR (n:{SINGLE_LAYER_ENTITY_LABEL}) REQUIRE (n.workspace, n.file_path, n.entity_name) IS UNIQUE"
            )

    async def _reset_driver(self) -> None:
        driver = self.driver
        self.driver = None
        if driver is not None:
            await driver.close()

    async def _reinitialize_driver(self) -> None:
        await self._reset_driver()
        await self.initialize()

    async def close(self) -> None:
        await self._reset_driver()

    async def _delete_document_graph_once(
        self, file_path: str, *, suppress_missing: bool
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, file_path: $file_path}})
                OPTIONAL MATCH (n)-[r:{SINGLE_LAYER_RELATION_TYPE}]-()
                RETURN count(DISTINCT n) AS entities, count(DISTINCT r) AS relations
                """,
                workspace=self.workspace,
                file_path=file_path,
            )
            summary = await result.single()
            entity_count = int((summary or {}).get("entities") or 0)
            relation_count = int((summary or {}).get("relations") or 0)
            if entity_count == 0:
                if suppress_missing:
                    return {"file_path": file_path, "deleted": False, "entities": 0, "relations": 0}
                raise ValueError(f"Document graph not found: {file_path}")
            await session.run(
                f"MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, file_path: $file_path}}) DETACH DELETE n",
                workspace=self.workspace,
                file_path=file_path,
            )
        return {
            "file_path": file_path,
            "deleted": True,
            "entities": entity_count,
            "relations": relation_count,
        }

    async def upsert_document_graph(self, graph: DocumentGraph) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")

        entity_rows = [
            {
                "workspace": graph.workspace,
                "node_id": node.node_id,
                "entity_name": node.entity_name,
                "entity_type": node.entity_type,
                "entity_description": node.entity_description,
                "file_path": node.file_path,
                "status": node.status,
                "library_id": node.library_id,
                "chunk_ids": node.chunk_ids,
                "aliases": node.aliases,
                "normalized_name": node.normalized_name,
                "alias_keys": node.alias_keys,
                "created_at": node.created_at,
            }
            for node in graph.entity_nodes
        ]
        relation_rows = [
            {
                "workspace": graph.workspace,
                "relation_id": edge.relation_id,
                "source_entity": edge.source_entity,
                "target_entity": edge.target_entity,
                "relationship_description": edge.relationship_description,
                "situation": edge.situation,
                "relation_keywords": edge.relation_keywords,
                "synonym": edge.synonym,
                "file_path": edge.file_path,
                "status": edge.status,
                "library_id": edge.library_id,
                "chunk_ids": edge.chunk_ids,
                "created_at": edge.created_at,
            }
            for edge in graph.relation_edges
        ]

        async with self.driver.session(database=self.database) as session:
            if entity_rows:
                await session.run(
                    f"""
                    UNWIND $rows AS row
                    MERGE (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: row.workspace, file_path: row.file_path, entity_name: row.entity_name}})
                    SET n.entity_name = row.entity_name,
                        n.node_id = row.node_id,
                        n.entity_type = row.entity_type,
                        n.entity_description = row.entity_description,
                        n.file_path = row.file_path,
                        n.status = row.status,
                        n.library_id = row.library_id,
                        n.chunk_ids = row.chunk_ids,
                        n.aliases = row.aliases,
                        n.normalized_name = row.normalized_name,
                        n.alias_keys = row.alias_keys,
                        n.created_at = row.created_at
                    """,
                    rows=entity_rows,
                )
            if relation_rows:
                await session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: row.workspace, file_path: row.file_path, entity_name: row.source_entity}})
                    MATCH (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: row.workspace, file_path: row.file_path, entity_name: row.target_entity}})
                    MERGE (src)-[r:{SINGLE_LAYER_RELATION_TYPE} {{
                        workspace: row.workspace,
                        relation_id: row.relation_id
                    }}]->(tgt)
                    SET r.source_entity = row.source_entity,
                        r.target_entity = row.target_entity,
                        r.relationship_description = row.relationship_description,
                        r.relation_id = row.relation_id,
                        r.situation = row.situation,
                        r.relation_keywords = row.relation_keywords,
                        r.synonym = row.synonym,
                        r.file_path = row.file_path,
                        r.status = row.status,
                        r.library_id = row.library_id,
                        r.chunk_ids = row.chunk_ids,
                        r.created_at = row.created_at
                    """,
                    rows=relation_rows,
                )

        return {
            "file_path": graph.file_path,
            "entity_nodes": len(graph.entity_nodes),
            "relation_edges": len(graph.relation_edges),
            "synonym_edges": sum(1 for edge in graph.relation_edges if edge.synonym),
        }

    async def upsert_synonym_relations(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")

        relation_rows = [row for row in rows if isinstance(row, dict)]
        if not relation_rows:
            return {"workspace": self.workspace, "synonym_edges": 0}

        async with self.driver.session(database=self.database) as session:
            await session.run(
                f"""
                UNWIND $rows AS row
                MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{
                    workspace: row.workspace,
                    file_path: row.source_file_path,
                    entity_name: row.source_entity
                }})
                MATCH (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{
                    workspace: row.workspace,
                    file_path: row.target_file_path,
                    entity_name: row.target_entity
                }})
                MERGE (src)-[r:{SINGLE_LAYER_RELATION_TYPE} {{
                    workspace: row.workspace,
                    relation_id: row.relation_id
                }}]->(tgt)
                ON CREATE SET r.source_entity = row.source_entity,
                              r.target_entity = row.target_entity,
                              r.relationship_description = row.relationship_description,
                              r.situation = row.situation,
                              r.relation_keywords = row.relation_keywords,
                              r.synonym = true,
                              r.file_path = row.file_path,
                              r.chunk_ids = row.chunk_ids,
                              r.created_at = timestamp()
                """,
                rows=relation_rows,
            )

        return {"workspace": self.workspace, "synonym_edges": len(relation_rows)}

    async def merge_fast_ingest_post_compile_edges(
        self,
        *,
        workspace: str,
        batch_id: str,
        edges: list[dict[str, Any]],
        compile_phase: str,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")

        rows: list[dict[str, Any]] = []
        entity_rows: list[dict[str, Any]] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            edge_id = str(edge.get("edge_id") or "").strip()
            doc_id = str(edge.get("doc_id") or "").strip()
            source_entity = str(edge.get("source_entity") or "").strip()
            target_entity = str(edge.get("target_entity") or "").strip()
            source_canonical_id = str(edge.get("source_canonical_id") or "").strip()
            target_canonical_id = str(edge.get("target_canonical_id") or "").strip()
            relation_type = str(edge.get("relation_type") or "").strip()
            relation_description = str(edge.get("relation_description") or "").strip()
            source_file_path = str(edge.get("source_file_path") or "").strip()
            if not source_file_path:
                source_file_path = str(edge.get("file_path") or "").strip()
            occurrence = max(1, int(edge.get("occurrence") or 1))
            source_refs = [
                str(item).strip() for item in (edge.get("source_refs") or []) if str(item).strip()
            ]
            chunk_ids = _dedupe_keep_order(
                [
                    ref.split(":", 1)[1]
                    for ref in source_refs
                    if ":" in ref and ref.split(":", 1)[1].strip()
                ]
            )
            relation_keywords = _derive_relation_keywords(
                source_entity,
                target_entity,
                relation_type or relation_description,
                relation_description,
                limit=6,
            )
            if (
                not edge_id
                or not doc_id
                or not source_entity
                or not target_entity
                or not source_file_path
                or not source_canonical_id
                or not target_canonical_id
            ):
                continue
            rows.append(
                {
                    "workspace": workspace,
                    "doc_id": doc_id,
                    "batch_id": batch_id,
                    "merge_token": str(edge.get("merge_token") or batch_id),
                    "compile_phase": str(compile_phase or "FAST_INGEST"),
                    "edge_id": edge_id,
                    "source_entity": source_entity,
                    "target_entity": target_entity,
                    "source_canonical_id": source_canonical_id,
                    "target_canonical_id": target_canonical_id,
                    "relation_type": relation_type or relation_description or "related_to",
                    "relation_description": relation_description or relation_type or "related_to",
                    "occ_increment": occurrence,
                    "new_refs": _dedupe_keep_order(source_refs),
                    "chunk_ids": chunk_ids,
                    "relation_keywords": relation_keywords,
                    "file_path": source_file_path,
                }
            )
            entity_rows.append(
                {
                    "workspace": workspace,
                    "doc_id": doc_id,
                    "file_path": source_file_path,
                    "entity_name": source_entity,
                    "normalized_name": _normalize_entity_lookup_key(source_entity),
                }
            )
            entity_rows.append(
                {
                    "workspace": workspace,
                    "doc_id": doc_id,
                    "file_path": source_file_path,
                    "entity_name": target_entity,
                    "normalized_name": _normalize_entity_lookup_key(target_entity),
                }
            )

        if not rows:
            return {
                "workspace": workspace,
                "batch_id": batch_id,
                "merged_edges": 0,
                "merged_entities": 0,
                "synonym_edges": 0,
            }

        entity_rows = [
            row
            for row in {
                (
                    item["workspace"],
                    item["doc_id"],
                    item["file_path"],
                    item["entity_name"],
                    item["normalized_name"],
                ): item
                for item in entity_rows
                if item["entity_name"] and item["normalized_name"]
            }.values()
        ]

        async with self.driver.session(database=self.database) as session:
            await session.run(
                f"""
                UNWIND $rows AS row
                MERGE (src:{SINGLE_LAYER_ENTITY_LABEL} {{
                    workspace: row.workspace,
                    file_path: row.file_path,
                    entity_name: row.source_entity
                }})
                ON CREATE SET src.node_id = row.source_canonical_id,
                              src.entity_type = 'Concept',
                              src.entity_description = row.source_entity,
                              src.status = 'PENDING_LINT',
                              src.chunk_ids = row.chunk_ids,
                              src.aliases = [],
                              src.normalized_name = toLower(row.source_entity),
                              src.alias_keys = [toLower(row.source_entity)],
                              src.canonical_id = row.source_canonical_id,
                              src.post_compile_source = $post_compile_source,
                              src.created_at = timestamp()
                ON MATCH SET src.canonical_id = row.source_canonical_id,
                             src.status = 'PENDING_LINT',
                             src.updated_at = timestamp(),
                             src.chunk_ids = reduce(acc = coalesce(src.chunk_ids, []), cid IN row.chunk_ids |
                                CASE WHEN cid IN acc THEN acc ELSE acc + cid END)
                MERGE (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{
                    workspace: row.workspace,
                    file_path: row.file_path,
                    entity_name: row.target_entity
                }})
                ON CREATE SET tgt.node_id = row.target_canonical_id,
                              tgt.entity_type = 'Concept',
                              tgt.entity_description = row.target_entity,
                              tgt.status = 'PENDING_LINT',
                              tgt.chunk_ids = row.chunk_ids,
                              tgt.aliases = [],
                              tgt.normalized_name = toLower(row.target_entity),
                              tgt.alias_keys = [toLower(row.target_entity)],
                              tgt.canonical_id = row.target_canonical_id,
                              tgt.post_compile_source = $post_compile_source,
                              tgt.created_at = timestamp()
                ON MATCH SET tgt.canonical_id = row.target_canonical_id,
                             tgt.status = 'PENDING_LINT',
                             tgt.updated_at = timestamp(),
                             tgt.chunk_ids = reduce(acc = coalesce(tgt.chunk_ids, []), cid IN row.chunk_ids |
                                CASE WHEN cid IN acc THEN acc ELSE acc + cid END)
                MERGE (src)-[r:{SINGLE_LAYER_RELATION_TYPE} {{
                    workspace: row.workspace,
                    relation_id: row.edge_id
                }}]->(tgt)
                ON CREATE SET r.source_entity = row.source_entity,
                              r.target_entity = row.target_entity,
                              r.relationship_description = row.relation_description,
                              r.situation = row.relation_description,
                              r.relation_keywords = row.relation_keywords,
                              r.synonym = false,
                              r.file_path = row.file_path,
                              r.chunk_ids = row.chunk_ids,
                              r.source_refs = row.new_refs,
                              r.occurrence = row.occ_increment,
                              r.status = 'PENDING_LINT',
                              r.compile_phase = row.compile_phase,
                              r.batch_id = row.batch_id,
                              r.post_compile_source = $post_compile_source,
                              r.doc_id = row.doc_id,
                              r.post_compile_batch_ids = [row.merge_token],
                              r.created_at = timestamp(),
                              r.updated_at = timestamp()
                ON MATCH SET r.relationship_description = CASE
                                    WHEN coalesce(r.relationship_description, '') = '' THEN row.relation_description
                                    WHEN row.relation_description = '' THEN r.relationship_description
                                    WHEN row.relation_description = r.relationship_description THEN r.relationship_description
                                    ELSE r.relationship_description + '\\n' + row.relation_description
                                END,
                             r.situation = CASE
                                    WHEN coalesce(r.situation, '') = '' THEN row.relation_description
                                    ELSE r.situation
                                END,
                             r.relation_keywords = reduce(acc = coalesce(r.relation_keywords, []), kw IN row.relation_keywords |
                                CASE WHEN kw IN acc THEN acc ELSE acc + kw END),
                             r.chunk_ids = reduce(acc = coalesce(r.chunk_ids, []), cid IN row.chunk_ids |
                                CASE WHEN cid IN acc THEN acc ELSE acc + cid END),
                             r.source_refs = reduce(acc = coalesce(r.source_refs, []), ref IN row.new_refs |
                                CASE WHEN ref IN acc THEN acc ELSE acc + ref END),
                             r.occurrence = CASE
                                WHEN row.merge_token IN coalesce(r.post_compile_batch_ids, [])
                                THEN coalesce(r.occurrence, 0)
                                ELSE coalesce(r.occurrence, 0) + row.occ_increment
                             END,
                             r.status = 'PENDING_LINT',
                             r.compile_phase = row.compile_phase,
                             r.batch_id = row.batch_id,
                             r.post_compile_batch_ids = CASE
                                WHEN row.merge_token IN coalesce(r.post_compile_batch_ids, [])
                                THEN coalesce(r.post_compile_batch_ids, [])
                                ELSE coalesce(r.post_compile_batch_ids, []) + row.merge_token
                             END,
                             r.post_compile_source = $post_compile_source,
                             r.doc_id = row.doc_id,
                             r.updated_at = timestamp()
                """,
                rows=rows,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
            )

            synonym_pairs: dict[str, dict[str, Any]] = {}
            synonym_result = await session.run(
                f"""
                UNWIND $entity_rows AS row
                MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{
                    workspace: row.workspace,
                    file_path: row.file_path,
                    entity_name: row.entity_name
                }})
                MATCH (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: row.workspace}})
                WHERE coalesce(tgt.normalized_name, '') = row.normalized_name
                  AND tgt.file_path <> row.file_path
                RETURN src.file_path AS source_file_path,
                       src.entity_name AS source_entity,
                       tgt.file_path AS target_file_path,
                       tgt.entity_name AS target_entity,
                       row.workspace AS workspace,
                       row.normalized_name AS normalized_name
                """,
                entity_rows=entity_rows,
            )
            async for record in synonym_result:
                source_file_path = str(record.get("source_file_path") or "").strip()
                source_entity = str(record.get("source_entity") or "").strip()
                target_file_path = str(record.get("target_file_path") or "").strip()
                target_entity = str(record.get("target_entity") or "").strip()
                normalized_name = str(record.get("normalized_name") or "").strip()
                if (
                    not source_file_path
                    or not target_file_path
                    or not source_entity
                    or not target_entity
                    or not normalized_name
                ):
                    continue
                left_file_path, left_entity, right_file_path, right_entity = (
                    source_file_path,
                    source_entity,
                    target_file_path,
                    target_entity,
                )
                if (right_file_path, right_entity) < (left_file_path, left_entity):
                    left_file_path, left_entity, right_file_path, right_entity = (
                        right_file_path,
                        right_entity,
                        left_file_path,
                        left_entity,
                    )
                relation_id = _md5_id(
                    "syn-",
                    workspace,
                    normalized_name,
                    left_file_path,
                    right_file_path,
                )
                synonym_pairs[relation_id] = {
                    "workspace": workspace,
                    "relation_id": relation_id,
                    "source_file_path": left_file_path,
                    "source_entity": left_entity,
                    "target_file_path": right_file_path,
                    "target_entity": right_entity,
                    "relationship_description": f"{left_entity} 与 {right_entity} 为跨资产同义实体",
                    "situation": "cross_asset_synonym",
                    "relation_keywords": ["synonym", normalized_name],
                    "file_path": "synonym://cross_asset",
                    "chunk_ids": [],
                }
            synonym_summary = await self.upsert_synonym_relations(list(synonym_pairs.values()))

            summary_result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(n.post_compile_source, '') = $post_compile_source
                OPTIONAL MATCH (n)-[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]->()
                RETURN count(DISTINCT n) AS entities
                """,
                workspace=workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
            )
            summary = await summary_result.single()

        return {
            "workspace": workspace,
            "batch_id": batch_id,
            "merged_edges": len(rows),
            "merged_entities": int((summary or {}).get("entities") or 0),
            "workspace_post_compile_relations": len(rows),
            "synonym_edges": int((synonym_summary or {}).get("synonym_edges") or 0),
        }

    async def clear_fast_ingest_post_compile_injected(
        self,
        *,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        target_workspace = workspace or self.workspace
        async with self.driver.session(database=self.database) as session:
            summary_result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(n.post_compile_source, '') = $post_compile_source
                OPTIONAL MATCH (n)-[r:{SINGLE_LAYER_RELATION_TYPE}]-()
                RETURN count(DISTINCT n) AS entities,
                       count(DISTINCT r) AS relations
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
            )
            summary = await summary_result.single()
            await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(n.post_compile_source, '') = $post_compile_source
                DETACH DELETE n
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
            )
            await session.run(
                f"""
                MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace}})
                WHERE coalesce(i.post_compile_source, '') = $post_compile_source
                DETACH DELETE i
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
            )
        return {
            "workspace": target_workspace,
            "deleted": True,
            "entities": int((summary or {}).get("entities") or 0),
            "relations": int((summary or {}).get("relations") or 0),
        }

    async def show_fast_ingest_post_compile_summary(
        self,
        *,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        target_workspace = workspace or self.workspace
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(n.post_compile_source, '') = $post_compile_source
                OPTIONAL MATCH (n)-[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]->()
                OPTIONAL MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace}})
                RETURN count(DISTINCT n) AS entity_count,
                       count(DISTINCT CASE WHEN coalesce(r.post_compile_source, '') = $post_compile_source THEN r END) AS relation_count,
                       count(DISTINCT CASE WHEN coalesce(i.post_compile_source, '') = $post_compile_source THEN i END) AS issue_count,
                       count(DISTINCT CASE WHEN coalesce(n.status, '') = 'PENDING_LINT' THEN n END) AS pending_entity_count,
                       count(DISTINCT CASE WHEN coalesce(n.status, '') = 'LINTING' THEN n END) AS linting_entity_count,
                       count(DISTINCT CASE WHEN coalesce(n.status, '') = 'VERIFIED' THEN n END) AS verified_entity_count,
                       count(DISTINCT CASE WHEN r.status = 'PENDING_LINT' THEN r END) AS pending_lint_count,
                       count(DISTINCT CASE WHEN r.status = 'LINTING' THEN r END) AS linting_relation_count,
                       count(DISTINCT CASE WHEN r.status = 'VERIFIED' THEN r END) AS verified_count
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
            )
            row = await result.single()
        return {
            "workspace": target_workspace,
            "entity_count": int((row or {}).get("entity_count") or 0),
            "relation_count": int((row or {}).get("relation_count") or 0),
            "issue_count": int((row or {}).get("issue_count") or 0),
            "pending_entity_count": int((row or {}).get("pending_entity_count") or 0),
            "linting_entity_count": int((row or {}).get("linting_entity_count") or 0),
            "verified_entity_count": int((row or {}).get("verified_entity_count") or 0),
            "pending_lint_count": int((row or {}).get("pending_lint_count") or 0),
            "linting_relation_count": int((row or {}).get("linting_relation_count") or 0),
            "verified_count": int((row or {}).get("verified_count") or 0),
        }

    async def list_lint_issues(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        target_workspace = workspace or self.workspace
        safe_limit = max(1, min(500, int(limit or 100)))
        safe_offset = max(0, int(offset or 0))
        status_filter = str(status or "").strip().upper()
        async with self.driver.session(database=self.database) as session:
            issue_result = await session.run(
                f"""
                MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace}})
                WHERE coalesce(i.post_compile_source, '') = $post_compile_source
                  AND ($status = '' OR coalesce(i.status, '') = $status)
                RETURN i.issue_id AS issue_id,
                       coalesce(i.item_type, '') AS item_type,
                       coalesce(i.item_id, '') AS item_id,
                       coalesce(i.issue_type, '') AS issue_type,
                       coalesce(i.title, '') AS title,
                       coalesce(i.detail, '') AS detail,
                       coalesce(i.evidence, {{}}) AS evidence,
                       coalesce(i.status, '') AS status,
                       coalesce(i.batch_id, '') AS batch_id,
                       coalesce(i.created_at, 0) AS created_at,
                       coalesce(i.updated_at, 0) AS updated_at
                ORDER BY coalesce(i.updated_at, i.created_at, 0) DESC
                SKIP $offset
                LIMIT $limit
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                status=status_filter,
                offset=safe_offset,
                limit=safe_limit,
            )
            issues: list[dict[str, Any]] = []
            async for record in issue_result:
                issues.append(record.data())
            count_result = await session.run(
                f"""
                MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace}})
                WHERE coalesce(i.post_compile_source, '') = $post_compile_source
                  AND ($status = '' OR coalesce(i.status, '') = $status)
                RETURN count(i) AS total
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                status=status_filter,
            )
            count_row = await count_result.single()
        return {
            "workspace": target_workspace,
            "status": status_filter or None,
            "limit": safe_limit,
            "offset": safe_offset,
            "total": int((count_row or {}).get("total") or 0),
            "issues": issues,
        }

    async def delete_lint_issue(
        self,
        *,
        workspace: str | None = None,
        issue_id: str,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        target_workspace = workspace or self.workspace
        normalized_issue_id = str(issue_id or "").strip()
        if not normalized_issue_id:
            raise ValueError("issue_id is required")
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                f"""
                MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace, issue_id: $issue_id}})
                WHERE coalesce(i.post_compile_source, '') = $post_compile_source
                RETURN count(i) AS matched
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                issue_id=normalized_issue_id,
            )
            row = await result.single()
            matched = int((row or {}).get("matched") or 0)
            if matched:
                await session.run(
                    f"""
                    MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace, issue_id: $issue_id}})
                    WHERE coalesce(i.post_compile_source, '') = $post_compile_source
                    DETACH DELETE i
                    """,
                    workspace=target_workspace,
                    post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                    issue_id=normalized_issue_id,
                )
        return {
            "workspace": target_workspace,
            "issue_id": normalized_issue_id,
            "deleted": matched > 0,
        }

    async def delete_fast_ingest_post_compile_asset(
        self,
        *,
        workspace: str | None = None,
        asset_id: str,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        target_workspace = workspace or self.workspace
        normalized_asset_id = str(asset_id or "").strip()
        if not normalized_asset_id:
            raise ValueError("asset_id is required")
        asset_prefix = f"{normalized_asset_id}:"

        async with self.driver.session(database=self.database) as session:
            relation_result = await session.run(
                f"""
                MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                      -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]->
                      (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(r.post_compile_source, '') = $post_compile_source
                  AND any(ref IN coalesce(r.source_refs, []) WHERE ref STARTS WITH $asset_prefix)
                RETURN collect(DISTINCT coalesce(r.relation_id, '')) AS relation_ids,
                       collect(DISTINCT coalesce(src.node_id, '')) AS source_node_ids,
                       collect(DISTINCT coalesce(tgt.node_id, '')) AS target_node_ids,
                       count(DISTINCT r) AS relation_count
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                asset_prefix=asset_prefix,
            )
            relation_row = await relation_result.single()
            relation_ids = [
                item
                for item in ((relation_row or {}).get("relation_ids") or [])
                if str(item or "").strip()
            ]
            source_node_ids = [
                item
                for item in ((relation_row or {}).get("source_node_ids") or [])
                if str(item or "").strip()
            ]
            target_node_ids = [
                item
                for item in ((relation_row or {}).get("target_node_ids") or [])
                if str(item or "").strip()
            ]
            affected_node_ids = list(dict.fromkeys(source_node_ids + target_node_ids))
            await session.run(
                f"""
                MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                      -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]->
                      (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(r.post_compile_source, '') = $post_compile_source
                  AND any(ref IN coalesce(r.source_refs, []) WHERE ref STARTS WITH $asset_prefix)
                DELETE r
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                asset_prefix=asset_prefix,
            )

            orphan_result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE n.node_id IN $affected_node_ids
                  AND coalesce(n.post_compile_source, '') = $post_compile_source
                  AND NOT (n)-[:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]-()
                RETURN collect(DISTINCT coalesce(n.node_id, '')) AS node_ids,
                       count(DISTINCT n) AS node_count
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                affected_node_ids=affected_node_ids,
            )
            orphan_row = await orphan_result.single()
            node_ids = [
                item
                for item in ((orphan_row or {}).get("node_ids") or [])
                if str(item or "").strip()
            ]
            await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE n.node_id IN $affected_node_ids
                  AND coalesce(n.post_compile_source, '') = $post_compile_source
                  AND NOT (n)-[:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]-()
                DELETE n
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                affected_node_ids=affected_node_ids,
            )

            if relation_ids or node_ids:
                await session.run(
                    f"""
                    MATCH (i:{LINT_ISSUE_LABEL} {{workspace: $workspace}})
                    WHERE coalesce(i.post_compile_source, '') = $post_compile_source
                      AND (
                        (coalesce(i.item_type, '') = 'relation' AND i.item_id IN $relation_ids)
                        OR (coalesce(i.item_type, '') = 'entity' AND i.item_id IN $node_ids)
                      )
                    DETACH DELETE i
                    """,
                    workspace=target_workspace,
                    post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                    relation_ids=relation_ids,
                    node_ids=node_ids,
                )

        return {
            "workspace": target_workspace,
            "asset_id": normalized_asset_id,
            "deleted_relations": int((relation_row or {}).get("relation_count") or 0),
            "deleted_orphan_nodes": int((orphan_row or {}).get("node_count") or 0),
            "matched_relation_ids": len(relation_ids),
            "matched_node_ids": len(node_ids),
        }

    async def delete_document_graph(
        self, file_path: str, *, suppress_missing: bool = False
    ) -> dict[str, Any]:
        return await run_with_neo4j_retry(
            lambda: self._delete_document_graph_once(
                file_path,
                suppress_missing=suppress_missing,
            ),
            reset_driver=self._reinitialize_driver,
            operation_name="delete_document_graph",
        )

    async def clear_workspace(self, workspace: str | None = None) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        target_workspace = workspace or self.workspace
        return await clear_kg_workspace(
            driver=self.driver,
            database=self.database,
            workspace=target_workspace,
            entity_label=SINGLE_LAYER_ENTITY_LABEL,
            relation_type=SINGLE_LAYER_RELATION_TYPE,
            lint_issue_label=LINT_ISSUE_LABEL,
        )

    async def clear_workspaces_by_prefix(self, workspace_prefix: str) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        return await clear_kg_workspaces_by_prefix(
            driver=self.driver,
            database=self.database,
            workspace_prefix=workspace_prefix,
            entity_label=SINGLE_LAYER_ENTITY_LABEL,
            relation_type=SINGLE_LAYER_RELATION_TYPE,
            lint_issue_label=LINT_ISSUE_LABEL,
        )

    async def show_document_graph(self, file_path: str) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        return await show_kg_document_graph(
            driver=self.driver,
            database=self.database,
            workspace=self.workspace,
            file_path=file_path,
            entity_label=SINGLE_LAYER_ENTITY_LABEL,
            relation_type=SINGLE_LAYER_RELATION_TYPE,
        )

    async def show_synonyms(
        self, *, file_path: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        return await show_kg_synonyms(
            driver=self.driver,
            database=self.database,
            workspace=self.workspace,
            file_path=file_path,
            limit=limit,
            entity_label=SINGLE_LAYER_ENTITY_LABEL,
            relation_type=SINGLE_LAYER_RELATION_TYPE,
        )

    async def show_graph_summary(self) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        return await show_kg_graph_summary(
            driver=self.driver,
            database=self.database,
            workspace=self.workspace,
            entity_label=SINGLE_LAYER_ENTITY_LABEL,
            relation_type=SINGLE_LAYER_RELATION_TYPE,
        )

    async def export_graph_records(
        self,
        *,
        workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.driver is None:
            raise RuntimeError("Repository not initialized")
        return await export_kg_repository_records(
            driver=self.driver,
            database=self.database,
            entity_label=SINGLE_LAYER_ENTITY_LABEL,
            relation_type=SINGLE_LAYER_RELATION_TYPE,
            workspaces=workspaces,
        )


class QueryKeywordExtractor:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self._client: AsyncOpenAI | None = None

    def _client_or_create(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def extract(self, query: str) -> QueryKeywords:
        fallback_keywords = _dedupe_keep_order([query] + _tokenize_search_text(query))
        if not self.api_key:
            return QueryKeywords(query=query, fallback_keywords=fallback_keywords)
        try:
            response = await self._client_or_create().chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                extra_body=extra_body_with_vllm_priority(priority=VLLM_PRIORITY_AGENT),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract retrieval keywords for a document-level knowledge graph search. "
                            "Return strict JSON with top-level keys entity_keywords and topic_keywords. "
                            "Prefer terminology that will likely appear in an English technical manual, even if the user asks in Chinese."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "query": query,
                                "rules": [
                                    "entity_keywords should focus on named modules, peripherals, subsystems, registers, or exact technical phrases.",
                                    "topic_keywords should focus on functions, behaviors, procedures, or chapter topics.",
                                    "Each list should contain 2 to 8 concise items.",
                                    "Do not include explanations.",
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            )
            payload = SingleLayerKGExtractor._parse_json(
                response.choices[0].message.content or "{}"
            )
            entity_keywords = _dedupe_keep_order(payload.get("entity_keywords") or [])
            topic_keywords = _dedupe_keep_order(payload.get("topic_keywords") or [])
            return QueryKeywords(
                query=query,
                entity_keywords=entity_keywords,
                topic_keywords=topic_keywords,
                fallback_keywords=fallback_keywords,
            )
        except Exception as exc:
            logger.warning("Keyword extraction failed for query '%s': %s", query, exc)
            return QueryKeywords(query=query, fallback_keywords=fallback_keywords)


class SingleLayerKGQueryEngine:
    def __init__(
        self,
        *,
        workspace: str,
        repository: SingleLayerKGRepository,
        vector_store: KGVectorStore | None,
        embedding_client: KGEmbeddingClient | None,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: str | None,
        llm_timeout: int = 120,
        query_synonym_expand_enabled: bool = True,
        query_synonym_expand_limit: int = 8,
        vector_entity_top_k: int = 20,
        vector_relation_top_k: int = 24,
        vector_chunk_top_k: int = 24,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self.vector_store = vector_store
        self.embedding_client = embedding_client
        self.keyword_extractor = QueryKeywordExtractor(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            timeout=llm_timeout,
        )
        self.query_synonym_expand_enabled = query_synonym_expand_enabled
        self.query_synonym_expand_limit = query_synonym_expand_limit
        self.vector_entity_top_k = vector_entity_top_k
        self.vector_relation_top_k = vector_relation_top_k
        self.vector_chunk_top_k = vector_chunk_top_k
        self._artifact_cache: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.keyword_extractor.close()

    async def query_document(
        self,
        *,
        file_path: str,
        query: str,
        artifacts_dir: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = True,
    ) -> QueryResult:
        normalized_file_path = str(Path(file_path).expanduser().resolve())
        artifact_payload = self._load_artifact_payload(normalized_file_path, artifacts_dir)
        retrieval_source = "neo4j"
        try:
            graph_payload = await self.repository.show_document_graph(normalized_file_path)
        except Exception:
            graph_payload = self._build_graph_payload_from_artifacts(artifact_payload)
            retrieval_source = "artifact"

        keywords = (
            await self.keyword_extractor.extract(query)
            if use_llm_keywords
            else QueryKeywords(
                query=query,
                fallback_keywords=_dedupe_keep_order([query] + _tokenize_search_text(query)),
            )
        )
        all_keywords = keywords.all_keywords()
        chunk_map = self._build_chunk_map(artifact_payload)

        entity_hits = self._score_entities(
            graph_payload.get("entities") or [],
            entity_keywords=keywords.entity_keywords or all_keywords,
            topic_keywords=keywords.topic_keywords or all_keywords,
            limit=top_entities,
        )
        relation_hits = self._score_relations(
            graph_payload.get("relations") or [],
            entity_hits=entity_hits,
            all_keywords=all_keywords,
            limit=top_relations,
        )
        chunk_hits = self._score_chunks(
            chunk_map=chunk_map,
            entity_hits=entity_hits,
            relation_hits=relation_hits,
            all_keywords=all_keywords,
            limit=top_chunks,
        )
        context_text = self._build_context_text(entity_hits, relation_hits, chunk_hits)
        stats = {
            "entity_candidates": len(graph_payload.get("entities") or []),
            "relation_candidates": len(graph_payload.get("relations") or []),
            "chunk_candidates": len(chunk_map),
            "returned_entities": len(entity_hits),
            "returned_relations": len(relation_hits),
            "returned_chunks": len(chunk_hits),
        }
        return QueryResult(
            workspace=self.workspace,
            file_path=normalized_file_path,
            query=query,
            artifact_dir=artifact_payload["artifact_dir"],
            query_scope="document",
            keywords=keywords,
            entities=entity_hits,
            relations=relation_hits,
            chunks=chunk_hits,
            context_text=context_text,
            retrieval_source=retrieval_source,
            stats=stats,
        )

    async def query_workspace(
        self,
        *,
        query: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = True,
    ) -> QueryResult:
        if self.vector_store is None or self.embedding_client is None:
            raise RuntimeError(
                "Workspace vector query requires KG vector store and embedding client"
            )

        keywords = (
            await self.keyword_extractor.extract(query)
            if use_llm_keywords
            else QueryKeywords(
                query=query,
                fallback_keywords=_dedupe_keep_order([query] + _tokenize_search_text(query)),
            )
        )
        all_keywords = keywords.all_keywords()
        query_text = "\n".join(_dedupe_keep_order([query] + all_keywords))
        query_vector = (await self.embedding_client.embed_texts([query_text]))[0]

        entity_rows = await self.vector_store.query_entities(
            query_vector=query_vector,
            workspace=self.workspace,
            top_k=max(top_entities, self.vector_entity_top_k),
            min_similarity=-1.0,
        )
        relation_rows = await self.vector_store.query_relations(
            query_vector=query_vector,
            workspace=self.workspace,
            top_k=max(top_relations, self.vector_relation_top_k),
            min_similarity=-1.0,
        )
        chunk_rows = await self.vector_store.query_chunks(
            query_vector=query_vector,
            workspace=self.workspace,
            top_k=max(top_chunks * 2, self.vector_chunk_top_k),
            min_similarity=-1.0,
        )

        entity_hits = self._build_vector_entity_hits(entity_rows, all_keywords)
        entity_hits = await self._expand_entity_synonyms(
            entity_hits, all_keywords=all_keywords, limit=top_entities
        )
        relation_hits = self._build_vector_relation_hits(
            relation_rows, entity_hits=entity_hits, all_keywords=all_keywords
        )
        chunk_hits = await self._build_vector_chunk_hits(
            chunk_rows=chunk_rows,
            entity_hits=entity_hits,
            relation_hits=relation_hits,
            all_keywords=all_keywords,
            limit=top_chunks,
        )

        entity_hits = entity_hits[:top_entities]
        relation_hits = relation_hits[:top_relations]
        stats = {
            "entity_candidates": len(entity_rows),
            "relation_candidates": len(relation_rows),
            "chunk_candidates": len(chunk_rows),
            "returned_entities": len(entity_hits),
            "returned_relations": len(relation_hits),
            "returned_chunks": len(chunk_hits),
            "synonym_expansion_enabled": self.query_synonym_expand_enabled,
        }
        return QueryResult(
            workspace=self.workspace,
            file_path="",
            query=query,
            artifact_dir="",
            query_scope="workspace",
            keywords=keywords,
            entities=entity_hits,
            relations=relation_hits,
            chunks=chunk_hits,
            context_text=self._build_context_text(entity_hits, relation_hits, chunk_hits),
            retrieval_source="kg_vector_pg",
            stats=stats,
        )

    def _build_vector_entity_hits(
        self,
        rows: list[dict[str, Any]],
        all_keywords: list[str],
    ) -> list[QueryEntityHit]:
        hits: list[QueryEntityHit] = []
        seen: set[str] = set()
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            item_id = str(row.get("item_id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            entity_name = str(metadata.get("entity_name") or row.get("content") or "")
            entity_type = str(metadata.get("entity_type") or "Concept")
            entity_description = str(metadata.get("entity_description") or row.get("content") or "")
            aliases = [str(alias) for alias in metadata.get("aliases") or [] if str(alias).strip()]
            lexical_score, lexical_terms = _score_keyword_matches(
                "\n".join([entity_name, entity_description] + aliases),
                all_keywords,
                phrase_weight=6.0,
                token_weight=2.0,
            )
            vector_score = float(row.get("similarity") or 0.0)
            hits.append(
                QueryEntityHit(
                    item_id=item_id,
                    entity_name=entity_name,
                    entity_type=entity_type,
                    entity_description=entity_description,
                    file_path=str(row.get("file_path") or metadata.get("file_path") or ""),
                    aliases=aliases,
                    chunk_ids=[
                        str(chunk_id)
                        for chunk_id in metadata.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    score=round(vector_score * 100 + lexical_score, 3),
                    vector_score=round(vector_score, 6),
                    normalized_name=str(
                        metadata.get("normalized_name") or _normalize_key(entity_name)
                    ),
                    matched_terms=lexical_terms,
                )
            )
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits

    def _build_vector_relation_hits(
        self,
        rows: list[dict[str, Any]],
        *,
        entity_hits: list[QueryEntityHit],
        all_keywords: list[str],
    ) -> list[QueryRelationHit]:
        entity_score_map = {item.entity_name: item.score for item in entity_hits}
        hits: list[QueryRelationHit] = []
        seen: set[str] = set()
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            relation_id = str(row.get("item_id") or metadata.get("relation_id") or "")
            if not relation_id or relation_id in seen:
                continue
            seen.add(relation_id)
            source_entity = str(metadata.get("source_entity") or "")
            target_entity = str(metadata.get("target_entity") or "")
            relation_description = str(
                metadata.get("relationship_description") or row.get("content") or ""
            )
            situation = str(metadata.get("situation") or "")
            relation_keywords = [
                str(item) for item in metadata.get("relation_keywords") or [] if str(item).strip()
            ]
            lexical_score, matched_terms = _score_keyword_matches(
                "\n".join(
                    [source_entity, target_entity, relation_description, situation]
                    + relation_keywords
                ),
                all_keywords,
                phrase_weight=4.0,
                token_weight=1.5,
            )
            support_score = (
                entity_score_map.get(source_entity, 0.0) * 0.2
                + entity_score_map.get(target_entity, 0.0) * 0.2
            )
            vector_score = float(row.get("similarity") or 0.0)
            hits.append(
                QueryRelationHit(
                    relation_id=relation_id,
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relationship_description=relation_description,
                    situation=situation,
                    relation_keywords=relation_keywords,
                    file_path=str(row.get("file_path") or metadata.get("file_path") or ""),
                    chunk_ids=[
                        str(chunk_id)
                        for chunk_id in metadata.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    score=round(vector_score * 100 + lexical_score + support_score, 3),
                    vector_score=round(vector_score, 6),
                    matched_terms=matched_terms,
                )
            )
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits

    async def _build_vector_chunk_hits(
        self,
        *,
        chunk_rows: list[dict[str, Any]],
        entity_hits: list[QueryEntityHit],
        relation_hits: list[QueryRelationHit],
        all_keywords: list[str],
        limit: int,
    ) -> list[QueryChunkHit]:
        chunk_map: dict[str, dict[str, Any]] = {
            str(row.get("item_id") or ""): row
            for row in chunk_rows
            if str(row.get("item_id") or "")
        }
        required_chunk_ids: set[str] = set()
        entity_support: dict[str, list[str]] = {}
        relation_support: dict[str, list[str]] = {}
        for entity in entity_hits:
            for chunk_id in entity.chunk_ids:
                required_chunk_ids.add(chunk_id)
                entity_support.setdefault(chunk_id, []).append(entity.entity_name)
        for relation in relation_hits:
            relation_name = f"{relation.source_entity} -> {relation.target_entity}"
            for chunk_id in relation.chunk_ids:
                required_chunk_ids.add(chunk_id)
                relation_support.setdefault(chunk_id, []).append(relation_name)
        missing_chunk_ids = [
            chunk_id for chunk_id in required_chunk_ids if chunk_id not in chunk_map
        ]
        if missing_chunk_ids:
            for row in await self.vector_store.get_chunks_by_ids(
                workspace=self.workspace, chunk_ids=missing_chunk_ids
            ):
                chunk_map[str(row.get("item_id") or "")] = row

        hits: list[QueryChunkHit] = []
        for chunk_id, row in chunk_map.items():
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            text = str(metadata.get("text") or row.get("content") or "")
            lexical_score, matched_terms = _score_keyword_matches(
                text,
                all_keywords,
                phrase_weight=3.5,
                token_weight=1.0,
            )
            entity_names = _dedupe_keep_order(entity_support.get(chunk_id, []))
            relation_names = _dedupe_keep_order(relation_support.get(chunk_id, []))
            support_score = len(entity_names) * 4.0 + len(relation_names) * 2.5
            vector_score = float(row.get("similarity") or 0.0)
            total_score = vector_score * 100 + lexical_score + support_score
            if total_score <= 0:
                continue
            hits.append(
                QueryChunkHit(
                    chunk_id=chunk_id,
                    file_path=str(row.get("file_path") or metadata.get("file_path") or ""),
                    order=int(metadata.get("order") or 0),
                    text=text,
                    score=round(total_score, 3),
                    vector_score=round(vector_score, 6),
                    entity_hits=entity_names,
                    relation_hits=relation_names,
                    matched_terms=matched_terms,
                    metadata=metadata,
                )
            )
        hits.sort(key=lambda item: (item.score, -item.order), reverse=True)
        return hits[:limit]

    async def _expand_entity_synonyms(
        self,
        entity_hits: list[QueryEntityHit],
        *,
        all_keywords: list[str],
        limit: int,
    ) -> list[QueryEntityHit]:
        if not self.query_synonym_expand_enabled or self.vector_store is None:
            return entity_hits
        expanded = list(entity_hits)
        seen = {item.item_id for item in entity_hits}
        for entity in entity_hits[:limit]:
            normalized_keys = _dedupe_keep_order(
                [entity.normalized_name] + [_normalize_key(alias) for alias in entity.aliases]
            )
            synonym_rows = await self.vector_store.find_entity_synonyms(
                workspace=self.workspace,
                normalized_keys=normalized_keys,
                exclude_ids=seen,
                limit=self.query_synonym_expand_limit,
            )
            for row in synonym_rows:
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                item_id = str(row.get("item_id") or "")
                if not item_id or item_id in seen:
                    continue
                file_path = str(row.get("file_path") or metadata.get("file_path") or "")
                if file_path == entity.file_path:
                    continue
                entity_name = str(metadata.get("entity_name") or row.get("content") or "")
                entity_description = str(
                    metadata.get("entity_description") or row.get("content") or ""
                )
                aliases = [
                    str(alias) for alias in metadata.get("aliases") or [] if str(alias).strip()
                ]
                lexical_score, matched_terms = _score_keyword_matches(
                    "\n".join([entity_name, entity_description] + aliases),
                    all_keywords,
                    phrase_weight=4.0,
                    token_weight=1.5,
                )
                expanded.append(
                    QueryEntityHit(
                        item_id=item_id,
                        entity_name=entity_name,
                        entity_type=str(metadata.get("entity_type") or "Concept"),
                        entity_description=entity_description,
                        file_path=file_path,
                        aliases=aliases,
                        chunk_ids=[
                            str(chunk_id)
                            for chunk_id in metadata.get("chunk_ids") or []
                            if str(chunk_id).strip()
                        ],
                        score=round(entity.score * 0.85 + lexical_score, 3),
                        vector_score=max(0.0, round(entity.vector_score * 0.85, 6)),
                        normalized_name=str(
                            metadata.get("normalized_name") or _normalize_key(entity_name)
                        ),
                        synonym_of=entity.entity_name,
                        matched_terms=matched_terms,
                    )
                )
                seen.add(item_id)
        expanded.sort(key=lambda item: item.score, reverse=True)
        return expanded

    def _load_artifact_payload(self, file_path: str, artifacts_dir: str) -> dict[str, Any]:
        artifact_dir = self._resolve_artifact_dir(file_path, artifacts_dir)
        cache_key = str(artifact_dir)
        cached = self._artifact_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = {
            "artifact_dir": str(artifact_dir),
            "document_graph": json.loads(
                (artifact_dir / "document_graph.json").read_text(encoding="utf-8")
            ),
            "entity_nodes": json.loads(
                (artifact_dir / "entity_nodes.json").read_text(encoding="utf-8")
            ),
            "relation_edges": json.loads(
                (artifact_dir / "relation_edges.json").read_text(encoding="utf-8")
            ),
        }
        self._artifact_cache[cache_key] = payload
        return payload

    @staticmethod
    def _resolve_artifact_dir(file_path: str, artifacts_dir: str) -> Path:
        root = Path(artifacts_dir).expanduser().resolve()
        if (root / "document_graph.json").exists():
            return root
        candidate = root / Path(file_path).stem
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Artifact directory not found for {file_path}: {root}")

    @staticmethod
    def _build_graph_payload_from_artifacts(artifact_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "file_path": artifact_payload["document_graph"].get("summary", {}).get("file_path")
            or "",
            "entities": artifact_payload.get("entity_nodes") or [],
            "relations": artifact_payload.get("relation_edges") or [],
        }

    @staticmethod
    def _build_chunk_map(artifact_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for chunk in artifact_payload.get("document_graph", {}).get("chunks", []) or []:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id:
                continue
            out[chunk_id] = {
                "chunk_id": chunk_id,
                "order": int(chunk.get("order") or 0),
                "text": str(chunk.get("text") or ""),
                "metadata": chunk.get("metadata")
                if isinstance(chunk.get("metadata"), dict)
                else {},
            }
        return out

    def _score_entities(
        self,
        entities: list[dict[str, Any]],
        *,
        entity_keywords: list[str],
        topic_keywords: list[str],
        limit: int,
    ) -> list[QueryEntityHit]:
        hits: list[QueryEntityHit] = []
        for entity in entities:
            entity_name = str(entity.get("entity_name") or "")
            if not entity_name:
                continue
            aliases = [str(alias) for alias in entity.get("aliases") or [] if str(alias).strip()]
            desc = str(entity.get("entity_description") or "")
            name_score, matched_terms = _score_keyword_matches(
                entity_name,
                entity_keywords,
                phrase_weight=14.0,
                token_weight=6.0,
            )
            alias_score = 0.0
            alias_terms: list[str] = []
            for alias in aliases:
                current_score, current_terms = _score_keyword_matches(
                    alias,
                    entity_keywords,
                    phrase_weight=12.0,
                    token_weight=5.0,
                )
                if current_score > alias_score:
                    alias_score = current_score
                    alias_terms = current_terms
            desc_score, desc_terms = _score_keyword_matches(
                desc,
                topic_keywords,
                phrase_weight=4.0,
                token_weight=1.5,
            )
            score = name_score + alias_score + desc_score
            if score <= 0:
                continue
            hits.append(
                QueryEntityHit(
                    item_id=str(
                        entity.get("node_id")
                        or _md5_id(
                            "ent-",
                            str(entity.get("file_path") or ""),
                            str(entity.get("entity_type") or "Concept"),
                            entity_name,
                        )
                    ),
                    entity_name=entity_name,
                    entity_type=str(entity.get("entity_type") or "Concept"),
                    entity_description=desc,
                    file_path=str(entity.get("file_path") or ""),
                    aliases=aliases,
                    chunk_ids=[
                        str(chunk_id)
                        for chunk_id in entity.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    score=round(score, 3),
                    normalized_name=str(
                        entity.get("normalized_name") or _normalize_key(entity_name)
                    ),
                    matched_terms=_dedupe_keep_order(matched_terms + alias_terms + desc_terms),
                )
            )
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    def _score_relations(
        self,
        relations: list[dict[str, Any]],
        *,
        entity_hits: list[QueryEntityHit],
        all_keywords: list[str],
        limit: int,
    ) -> list[QueryRelationHit]:
        entity_score_map = {item.entity_name: item.score for item in entity_hits}
        hits: list[QueryRelationHit] = []
        for relation in relations:
            source_entity = str(relation.get("source_entity") or "")
            target_entity = str(relation.get("target_entity") or "")
            if not source_entity or not target_entity:
                continue
            source_score = entity_score_map.get(source_entity, 0.0)
            target_score = entity_score_map.get(target_entity, 0.0)
            relation_desc = str(relation.get("relationship_description") or "")
            situation = str(relation.get("situation") or "")
            relation_keywords = [
                str(item) for item in relation.get("relation_keywords") or [] if str(item).strip()
            ]
            desc_score, desc_terms = _score_keyword_matches(
                "\n".join([relation_desc] + relation_keywords),
                all_keywords,
                phrase_weight=5.0,
                token_weight=1.5,
            )
            situation_score, situation_terms = _score_keyword_matches(
                situation,
                all_keywords,
                phrase_weight=3.0,
                token_weight=1.0,
            )
            score = source_score * 0.45 + target_score * 0.45 + desc_score + situation_score
            if score <= 0:
                continue
            hits.append(
                QueryRelationHit(
                    relation_id=str(
                        relation.get("relation_id")
                        or _md5_id(
                            "rel-",
                            str(relation.get("file_path") or ""),
                            source_entity,
                            target_entity,
                            relation_desc,
                            situation,
                        )
                    ),
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relationship_description=relation_desc,
                    situation=situation,
                    relation_keywords=relation_keywords,
                    file_path=str(relation.get("file_path") or ""),
                    chunk_ids=[
                        str(chunk_id)
                        for chunk_id in relation.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    score=round(score, 3),
                    matched_terms=_dedupe_keep_order(desc_terms + situation_terms),
                )
            )
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    def _score_chunks(
        self,
        *,
        chunk_map: dict[str, dict[str, Any]],
        entity_hits: list[QueryEntityHit],
        relation_hits: list[QueryRelationHit],
        all_keywords: list[str],
        limit: int,
    ) -> list[QueryChunkHit]:
        entity_support: dict[str, list[str]] = {}
        for entity in entity_hits:
            for chunk_id in entity.chunk_ids:
                if chunk_id in chunk_map:
                    entity_support.setdefault(chunk_id, []).append(entity.entity_name)

        relation_support: dict[str, list[str]] = {}
        for relation in relation_hits:
            relation_name = f"{relation.source_entity} -> {relation.target_entity}"
            for chunk_id in relation.chunk_ids:
                if chunk_id in chunk_map:
                    relation_support.setdefault(chunk_id, []).append(relation_name)

        candidate_chunk_ids = set(entity_support) | set(relation_support)
        if not candidate_chunk_ids:
            candidate_chunk_ids = set(chunk_map.keys())

        hits: list[QueryChunkHit] = []
        for chunk_id in candidate_chunk_ids:
            chunk = chunk_map.get(chunk_id)
            if chunk is None:
                continue
            text_score, matched_terms = _score_keyword_matches(
                str(chunk.get("text") or ""),
                all_keywords,
                phrase_weight=3.5,
                token_weight=1.0,
            )
            entity_names = _dedupe_keep_order(entity_support.get(chunk_id, []))
            relation_names = _dedupe_keep_order(relation_support.get(chunk_id, []))
            support_score = len(entity_names) * 4.0 + len(relation_names) * 2.5
            total_score = support_score + text_score
            if total_score <= 0:
                continue
            hits.append(
                QueryChunkHit(
                    chunk_id=chunk_id,
                    file_path=str(chunk.get("file_path") or ""),
                    order=int(chunk.get("order") or 0),
                    text=str(chunk.get("text") or ""),
                    score=round(total_score, 3),
                    entity_hits=entity_names,
                    relation_hits=relation_names,
                    matched_terms=matched_terms,
                    metadata=chunk.get("metadata")
                    if isinstance(chunk.get("metadata"), dict)
                    else {},
                )
            )

        hits.sort(key=lambda item: (item.score, -item.order), reverse=True)
        if len(hits) >= limit:
            return hits[:limit]

        existing_ids = {item.chunk_id for item in hits}
        lexical_fallback: list[QueryChunkHit] = []
        for chunk_id, chunk in chunk_map.items():
            if chunk_id in existing_ids:
                continue
            text_score, matched_terms = _score_keyword_matches(
                str(chunk.get("text") or ""),
                all_keywords,
                phrase_weight=3.5,
                token_weight=1.0,
            )
            if text_score <= 0:
                continue
            lexical_fallback.append(
                QueryChunkHit(
                    chunk_id=chunk_id,
                    file_path=str(chunk.get("file_path") or ""),
                    order=int(chunk.get("order") or 0),
                    text=str(chunk.get("text") or ""),
                    score=round(text_score, 3),
                    matched_terms=matched_terms,
                    metadata=chunk.get("metadata")
                    if isinstance(chunk.get("metadata"), dict)
                    else {},
                )
            )
        lexical_fallback.sort(key=lambda item: (item.score, -item.order), reverse=True)
        return (hits + lexical_fallback)[:limit]

    @staticmethod
    def _build_context_text(
        entity_hits: list[QueryEntityHit],
        relation_hits: list[QueryRelationHit],
        chunk_hits: list[QueryChunkHit],
    ) -> str:
        lines: list[str] = []
        if entity_hits:
            lines.append("[Entities]")
            for entity in entity_hits:
                lines.append(
                    f"- {entity.entity_name} ({entity.entity_type}) | score={entity.score} | {entity.entity_description}"
                )
        if relation_hits:
            lines.append("[Relations]")
            for relation in relation_hits:
                lines.append(
                    f"- {relation.source_entity} -> {relation.target_entity} | score={relation.score} | {relation.relationship_description}"
                )
        if chunk_hits:
            lines.append("[Chunks]")
            for chunk in chunk_hits:
                preview = _truncate_text(chunk.text.replace("\n", " "), 280)
                lines.append(
                    f"- {chunk.chunk_id} | order={chunk.order} | score={chunk.score} | {preview}"
                )
        return "\n".join(lines)


class SingleLayerKGService:
    """Single-layer KG build/query service for one workspace."""

    def __init__(self, config: HyperRAGConfig) -> None:
        self.config = config
        self.workspace = config.workspace or "default"
        self.extractors: dict[str, SingleLayerKGExtractor] = {}
        self.query_engine: SingleLayerKGQueryEngine | None = None
        self.mix_query_engine: Any | None = None
        self.embedding_client: KGEmbeddingClient | None = None
        self.multimodal_embedder: Any | None = None
        self.vector_store: KGVectorStore | None = None
        self.repository = SingleLayerKGRepository(workspace=self.workspace)
        self._last_extraction_stats: dict[str, Any] = {
            "mode": "llm",
            "structured_chunk_count": 0,
            "fallback_chunk_count": 0,
            "schema_hit_count": 0,
        }

    @classmethod
    def from_config_path(
        cls,
        config_path: str = "hyperrag.yml",
        *,
        workspace: str | None = None,
    ) -> SingleLayerKGService:
        config = load_config_from_yaml(config_path)
        if workspace:
            config.workspace = workspace
        return cls(config)

    def _get_extractor(
        self,
        *,
        extraction_mode: Literal["standard", "pre_compile", "fast_ingest"] = "standard",
    ) -> SingleLayerKGExtractor:
        extractor = self.extractors.get(extraction_mode)
        if extractor is not None:
            return extractor

        prompt_mode = "compact" if extraction_mode == "fast_ingest" else "full"
        desc_max_chars = int(getattr(self.config, "fast_ingest_er_description_max_chars", 64) or 64)
        if prompt_mode == "full":
            desc_max_chars = max(desc_max_chars, 300)

        extractor = SingleLayerKGExtractor(
            model=self.config.llm_model,
            base_url=self.config.llm_base_url,
            api_key=self.config.llm_api_key,
            timeout=self.config.llm_timeout,
            max_concurrent=self.config.ER_extraction_max_async,
            entity_types=self.config.entity_types or list(DEFAULT_ENTITY_TYPES),
            prompt_mode=prompt_mode,
            max_output_tokens=max(
                32,
                int(getattr(self.config, "fast_ingest_er_max_output_tokens", 512) or 512),
            ),
            entity_description_max_chars=desc_max_chars,
            retry_full_on_empty=bool(
                getattr(self.config, "fast_ingest_er_retry_full_on_empty", False)
            ),
            strict_required_fields=prompt_mode == "full",
            required_field_retry_attempts=3 if prompt_mode == "full" else 1,
            allow_entity_description_fallback=prompt_mode != "full",
            allow_relation_entity_inference=prompt_mode != "full",
        )
        self.extractors[extraction_mode] = extractor
        return extractor

    def _get_query_engine(self) -> SingleLayerKGQueryEngine:
        if self.query_engine is None:
            self.query_engine = SingleLayerKGQueryEngine(
                workspace=self.workspace,
                repository=self.repository,
                vector_store=self._get_vector_store() if self._vector_enabled() else None,
                embedding_client=self._get_embedding_client() if self._vector_enabled() else None,
                llm_model=self.config.llm_model,
                llm_base_url=self.config.llm_base_url,
                llm_api_key=self.config.llm_api_key,
                llm_timeout=self.config.llm_timeout,
                query_synonym_expand_enabled=self.config.query_synonym_expand_enabled,
                query_synonym_expand_limit=self.config.query_synonym_expand_limit,
                vector_entity_top_k=self.config.kg_vector_entity_top_k,
                vector_relation_top_k=self.config.kg_vector_relation_top_k,
                vector_chunk_top_k=self.config.kg_vector_chunk_top_k,
            )
        return self.query_engine

    def _get_mix_query_engine(self):
        if self.mix_query_engine is None:
            from .mix_query import SingleModeMixQueryEngine

            self.mix_query_engine = SingleModeMixQueryEngine(
                workspace=self.workspace,
                vector_store=self._get_vector_store(),
                embedding_client=self._get_embedding_client(),
                synonym_relation_writer=self.repository.upsert_synonym_relations,
                query_synonym_expand_enabled=self.config.query_synonym_expand_enabled,
                query_synonym_expand_limit=self.config.query_synonym_expand_limit,
                vector_entity_top_k=self.config.kg_vector_entity_top_k,
                vector_relation_top_k=self.config.kg_vector_relation_top_k,
                vector_chunk_top_k=self.config.kg_vector_chunk_top_k,
            )
        return self.mix_query_engine

    async def initialize(self) -> None:
        initialize_share_data(workers=1)
        self._configure_postgres_env()
        await self.repository.initialize()
        if self._vector_enabled():
            await self._get_vector_store().initialize()

    async def close(self) -> None:
        for extractor in self.extractors.values():
            await extractor.close()
        if self.query_engine is not None:
            await self.query_engine.close()
        if self.mix_query_engine is not None:
            await self.mix_query_engine.close()
        if self.embedding_client is not None:
            await self.embedding_client.close()
        if self.vector_store is not None:
            await self.vector_store.finalize()
        await self.repository.close()

    async def build_document_graph(
        self,
        *,
        input_path: str,
        mineru_dir: str | None = None,
        parser_params: dict[str, Any] | None = None,
        artifacts_dir: str | None = None,
        persist_to_kg: bool = True,
        persist_chunk_vectors: bool | None = None,
        include_relation_edges: bool = False,
        pre_compile_mode: bool = False,
        retry_er_chunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        resolved_file_path = index_storage_path(input_path)
        asset, segments, chunks = await self._parse_and_chunk_document(
            input_path=input_path,
            mineru_dir=mineru_dir,
            parser_params=parser_params,
        )
        return await self._build_graph_from_asset_segments(
            file_path=resolved_file_path,
            asset=asset,
            segments=segments,
            chunks=chunks,
            artifacts_dir=artifacts_dir,
            persist_to_kg=persist_to_kg,
            persist_chunk_vectors=persist_chunk_vectors,
            include_relation_edges=include_relation_edges,
            pre_compile_mode=pre_compile_mode,
            retry_er_chunk_ids=retry_er_chunk_ids,
        )

    async def _build_graph_from_asset_segments(
        self,
        *,
        file_path: str,
        asset: Asset,
        segments: list[Segment],
        chunks: list[ChunkRecord],
        artifacts_dir: str | None = None,
        persist_to_kg: bool = True,
        persist_chunk_vectors: bool | None = None,
        include_relation_edges: bool = False,
        pre_compile_mode: bool = False,
        retry_er_chunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        resolved_file_path = index_storage_path(file_path)
        logger.info(
            "SingleLayerKG: start graph build for asset=%s file=%s segments=%s chunks=%s",
            asset.doc_id,
            resolved_file_path,
            len(segments),
            len(chunks),
        )
        persist_chunk_vectors = (
            persist_to_kg if persist_chunk_vectors is None else bool(persist_chunk_vectors)
        )
        effective_artifacts_dir = None if pre_compile_mode else artifacts_dir
        requested_retry_chunk_ids = {
            str(chunk_id).strip()
            for chunk_id in (retry_er_chunk_ids or [])
            if str(chunk_id).strip()
        }
        missing_retry_chunk_ids: list[str] = []
        if requested_retry_chunk_ids:
            chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
            missing_retry_chunk_ids = sorted(requested_retry_chunk_ids - set(chunks_by_id))
            chunks = [chunk for chunk in chunks if chunk.chunk_id in requested_retry_chunk_ids]

        if persist_to_kg and not pre_compile_mode:
            await self.repository.delete_document_graph(resolved_file_path, suppress_missing=True)
            if self._vector_enabled():
                await self._get_vector_store().delete_by_file_path(
                    workspace=self.workspace, file_path=resolved_file_path
                )
        elif persist_chunk_vectors and not pre_compile_mode and self._vector_enabled():
            await self._get_vector_store().delete_chunks_by_file_path(
                workspace=self.workspace, file_path=resolved_file_path
            )

        parsing_chunking_sec = 0.0

        extract_started = time.monotonic()
        _emit_progress(
            f"SingleLayerKG: extracting entities/relations for asset={asset.doc_id} from {len(chunks)} chunks"
        )
        extraction_mode: Literal["standard", "pre_compile", "fast_ingest"]
        if include_relation_edges:
            extraction_mode = "fast_ingest"
        elif pre_compile_mode:
            extraction_mode = "pre_compile"
        else:
            extraction_mode = "standard"
        (
            extracted_entities,
            extracted_relations,
            failed_er_chunks,
        ) = await self._extract_entities_and_relations(
            asset_id=asset.doc_id,
            chunks=chunks,
            segments=segments,
            modality=asset.modality,
            title=asset.title or Path(resolved_file_path).name,
            extraction_mode=extraction_mode,
        )
        failed_er_chunks.extend(
            {
                "chunk_id": chunk_id,
                "error": "chunk ID was not found after document chunking",
            }
            for chunk_id in missing_retry_chunk_ids
        )
        extraction_sec = time.monotonic() - extract_started
        _emit_progress(
            f"SingleLayerKG: extracted entities/relations for asset={asset.doc_id} "
            f"entities={len(extracted_entities)} relations={len(extracted_relations)} elapsed={extraction_sec:.2f}s"
        )

        resolver = DocScopedSynonymResolver(
            workspace=self.workspace,
            merge_summary_helper=self._build_entity_merge_summary_helper(),
        )
        resolve_started = time.monotonic()
        _emit_progress(
            f"SingleLayerKG: resolving graph nodes for asset={asset.doc_id} "
            f"extractions={len(extracted_entities)}/{len(extracted_relations)}"
        )
        entity_nodes, relation_edges, _, resolution_metrics = await resolver.resolve(
            file_path=resolved_file_path,
            entities=extracted_entities,
            relations=extracted_relations,
        )
        resolution_sec = time.monotonic() - resolve_started
        _emit_progress(
            f"SingleLayerKG: resolved graph for asset={asset.doc_id} "
            f"entity_nodes={len(entity_nodes)} relation_edges={len(relation_edges)} elapsed={resolution_sec:.2f}s"
        )

        graph = DocumentGraph(
            workspace=self.workspace,
            file_path=resolved_file_path,
            source_uri=str(asset.source_uri),
            modality=asset.modality,
            title=asset.title or Path(resolved_file_path).name,
            asset=asset.model_dump(),
            segments=[segment.model_dump() for segment in segments],
            chunks=chunks,
            extracted_entities=extracted_entities,
            extracted_relations=extracted_relations,
            entity_nodes=entity_nodes,
            relation_edges=relation_edges,
        )

        if pre_compile_mode:
            self._mark_graph_as_precompiled(graph)

        artifact_paths: dict[str, str] = {}
        if effective_artifacts_dir:
            artifact_paths = await asyncio.to_thread(
                self._write_artifacts, graph, Path(effective_artifacts_dir)
            )

        neo4j_upsert_sec = 0.0
        storage_summary: dict[str, Any] = {}
        if persist_to_kg:
            neo4j_started = time.monotonic()
            _emit_progress(f"SingleLayerKG: persisting graph to Neo4j for asset={asset.doc_id}")
            storage_summary = await self.repository.upsert_document_graph(graph)
            neo4j_upsert_sec = time.monotonic() - neo4j_started
            _emit_progress(
                f"SingleLayerKG: persisted graph to Neo4j for asset={asset.doc_id} elapsed={neo4j_upsert_sec:.2f}s"
            )
        else:
            storage_summary = {"persisted": False, "route": "fast_ingest_post_compile"}

        vectorization_sec = 0.0
        vector_upsert_sec = 0.0
        vector_summary: dict[str, Any] = {}
        if self._vector_enabled():
            chunk_rows: list[dict[str, Any]] = []
            entity_rows: list[dict[str, Any]] = []
            relation_rows: list[dict[str, Any]] = []
            vectorize_started = time.monotonic()
            if persist_chunk_vectors and not pre_compile_mode:
                chunk_rows = await self._build_chunk_vector_rows(graph)
            if persist_to_kg:
                entity_rows, relation_rows = await self._build_graph_vector_rows(graph)
            vectorization_sec = time.monotonic() - vectorize_started

            if chunk_rows or entity_rows or relation_rows:
                _emit_progress(
                    f"SingleLayerKG: built vector rows for asset={asset.doc_id} "
                    f"chunk_vectors={len(chunk_rows)} entity_vectors={len(entity_rows)} "
                    f"relation_vectors={len(relation_rows)} elapsed={vectorization_sec:.2f}s"
                )
                vector_upsert_started = time.monotonic()
                _emit_progress(f"SingleLayerKG: persisting vectors for asset={asset.doc_id}")
                store = self._get_vector_store()
                if chunk_rows:
                    await store.upsert_chunks(chunk_rows)
                if entity_rows:
                    await store.upsert_entities(entity_rows)
                if relation_rows:
                    await store.upsert_relations(relation_rows)
                vector_upsert_sec = time.monotonic() - vector_upsert_started
                _emit_progress(
                    f"SingleLayerKG: persisted vectors for asset={asset.doc_id} elapsed={vector_upsert_sec:.2f}s"
                )
                vector_summary = {
                    "chunk_vectors": len(chunk_rows),
                    "entity_vectors": len(entity_rows),
                    "relation_vectors": len(relation_rows),
                    "schema": self.config.kg_vector_schema,
                }

        document_summary = await self._generate_document_summary(
            asset=asset,
            chunks=chunks,
        )

        total_sec = time.monotonic() - started_at
        chunk_count = max(1, len(chunks))
        graph_insert_total_sec = (
            resolution_sec + neo4j_upsert_sec + vectorization_sec + vector_upsert_sec
        )
        timings = {
            "total_sec": round(total_sec, 3),
            "parsing_chunking_sec": round(parsing_chunking_sec, 3),
            "entity_relation_extraction_sec": round(extraction_sec, 3),
            "entity_resolution_sec": round(resolution_sec, 3),
            "neo4j_upsert_sec": round(neo4j_upsert_sec, 3),
            "vectorization_sec": round(vectorization_sec, 3),
            "vector_upsert_sec": round(vector_upsert_sec, 3),
            "graph_insert_total_sec": round(graph_insert_total_sec, 3),
            "avg_graph_insert_per_chunk_sec": round(graph_insert_total_sec / chunk_count, 3),
        }
        counters = {
            "chunks": len(chunks),
            "failed_er_chunk_count": len(failed_er_chunks),
            "extracted_entities": len(extracted_entities),
            "extracted_relations": len(extracted_relations),
            "entity_nodes": len(entity_nodes),
            "relation_edges": len(relation_edges),
            "extraction_mode": str(self._last_extraction_stats.get("mode") or "llm"),
            "structured_chunk_count": int(
                self._last_extraction_stats.get("structured_chunk_count") or 0
            ),
            "fallback_chunk_count": int(
                self._last_extraction_stats.get("fallback_chunk_count") or 0
            ),
            "schema_hit_count": int(self._last_extraction_stats.get("schema_hit_count") or 0),
            **resolution_metrics,
        }
        segment_chunk_ids = _build_video_segment_chunk_ids(asset.modality, chunks)
        _emit_progress(
            f"SingleLayerKG: stage summary asset={asset.doc_id} chunks={counters['chunks']} "
            f"extracted_entities={counters['extracted_entities']} extracted_relations={counters['extracted_relations']} "
            f"entity_nodes={counters['entity_nodes']} relation_edges={counters['relation_edges']} "
            f"parsing_chunking_sec={parsing_chunking_sec:.2f}s entity_relation_extraction_sec={extraction_sec:.2f}s "
            f"entity_resolution_sec={resolution_sec:.2f}s neo4j_upsert_sec={neo4j_upsert_sec:.2f}s "
            f"vectorization_sec={vectorization_sec:.2f}s vector_upsert_sec={vector_upsert_sec:.2f}s total_sec={total_sec:.2f}s"
        )
        return {
            "summary": graph.to_summary(),
            "document_summary": document_summary,
            "storage": storage_summary,
            "vector_storage": vector_summary,
            "artifacts": artifact_paths,
            "timings": timings,
            "counters": counters,
            "persist_to_kg": persist_to_kg,
            "pre_compile_mode": pre_compile_mode,
            "failed_er_chunks": failed_er_chunks,
            "segment_chunk_ids": segment_chunk_ids,
            "relation_edges": [asdict(edge) for edge in relation_edges]
            if include_relation_edges
            else [],
        }

    @staticmethod
    def _mark_graph_as_precompiled(graph: DocumentGraph) -> None:
        graph.file_path = PRECOMPILED_FILE_PATH
        for node in graph.entity_nodes:
            node.file_path = PRECOMPILED_FILE_PATH
            node.status = "PENDING_LINT"
            node.library_id = PRECOMPILED_LIBRARY_ID
            node.chunk_ids = [PRECOMPILED_CHUNK_ID]
        for edge in graph.relation_edges:
            edge.file_path = PRECOMPILED_FILE_PATH
            edge.status = "PENDING_LINT"
            edge.library_id = PRECOMPILED_LIBRARY_ID
            edge.chunk_ids = [PRECOMPILED_CHUNK_ID]

    async def _generate_document_summary(
        self,
        *,
        asset: Asset,
        chunks: list[ChunkRecord],
    ) -> str | None:
        if not chunks:
            logger.info(
                "SingleLayerKG: skip document summary for asset=%s because chunks are empty",
                asset.doc_id,
            )
            return None

        summary_max_tokens = max(64, int(self.config.summary_max_tokens or 500))
        context_budget = max(2000, int(self.config.summary_context_size or 4096) * 4)
        chunk_texts: list[str] = []
        total_chars = 0
        for chunk in chunks:
            text = _normalize_text(chunk.text)
            if not text:
                continue
            if total_chars >= context_budget:
                break
            remaining = context_budget - total_chars
            snippet = text[:remaining].strip()
            if not snippet:
                continue
            chunk_texts.append(snippet)
            total_chars += len(snippet) + 2

        if not chunk_texts:
            logger.info(
                "SingleLayerKG: skip document summary for asset=%s because chunk text is empty",
                asset.doc_id,
            )
            return None

        title = str(asset.title or Path(str(asset.source_uri or asset.doc_id)).name)
        prompt = "\n".join(
            [
                f"知识库: {self.workspace}",
                f"文档标题: {title}",
                f"文档类型: {asset.modality}",
                "请基于下面的文档内容生成一个面向知识库检索的中文摘要。",
                "要求:",
                "1. 用 3 到 5 句话概括文档的主题、关键对象、核心步骤或结论。",
                "2. 保留重要术语、型号、部件、流程名和约束条件。",
                "3. 不要写套话，不要输出项目符号，不要虚构原文没有的信息。",
                "4. 输出纯文本摘要，长度控制在 120 到 220 个中文字符左右。",
                "文档内容:",
                "\n\n".join(chunk_texts),
            ]
        )

        try:
            logger.info("SingleLayerKG: generating document summary for asset=%s", asset.doc_id)
            summary = await self._get_extractor().complete_text(
                prompt,
                system_prompt="You generate concise Chinese retrieval summaries for indexed documents.",
                max_tokens=summary_max_tokens,
            )
            summary = _normalize_text(summary)
            if not summary:
                logger.warning("SingleLayerKG: empty document summary for asset=%s", asset.doc_id)
                return None
            logger.info("SingleLayerKG: generated document summary for asset=%s", asset.doc_id)
            return summary
        except Exception as exc:
            logger.warning(
                "SingleLayerKG: failed to generate document summary for asset=%s: %s",
                asset.doc_id,
                exc,
            )
            return None

    async def index_document(
        self,
        *,
        input_path: str,
        mineru_dir: str | None = None,
        parser_params: dict[str, Any] | None = None,
        artifacts_dir: str | None = None,
        persist_to_kg: bool = True,
        persist_chunk_vectors: bool | None = None,
        include_relation_edges: bool = False,
        pre_compile_mode: bool = False,
        retry_er_chunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.build_document_graph(
            input_path=input_path,
            mineru_dir=mineru_dir,
            parser_params=parser_params,
            artifacts_dir=artifacts_dir,
            persist_to_kg=persist_to_kg,
            persist_chunk_vectors=persist_chunk_vectors,
            include_relation_edges=include_relation_edges,
            pre_compile_mode=pre_compile_mode,
            retry_er_chunk_ids=retry_er_chunk_ids,
        )

    async def index_preparsed_document(
        self,
        *,
        file_path: str,
        asset_payload: dict[str, Any],
        segments_payload: list[dict[str, Any]],
        artifacts_dir: str | None = None,
        persist_to_kg: bool = True,
        persist_chunk_vectors: bool | None = None,
        include_relation_edges: bool = False,
        pre_compile_mode: bool = False,
        retry_er_chunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        resolved_file_path = index_storage_path(file_path)
        asset_data = dict(asset_payload or {})
        asset_data["source_uri"] = asset_data.get("source_uri") or resolved_file_path
        asset_data["segments_count"] = int(
            asset_data.get("segments_count") or len(segments_payload or [])
        )
        asset = Asset.model_validate(asset_data)
        segments = [Segment.model_validate(item) for item in segments_payload or []]
        chunks = await self._chunk_document(
            file_path=resolved_file_path,
            asset=asset,
            segments=segments,
        )
        logger.info(
            "SingleLayerKG: chunking prepared asset=%s segments=%s chunks=%s",
            asset.doc_id,
            len(segments),
            len(chunks),
        )
        return await self._build_graph_from_asset_segments(
            file_path=resolved_file_path,
            asset=asset,
            segments=segments,
            chunks=chunks,
            artifacts_dir=artifacts_dir,
            persist_to_kg=persist_to_kg,
            persist_chunk_vectors=persist_chunk_vectors,
            include_relation_edges=include_relation_edges,
            pre_compile_mode=pre_compile_mode,
            retry_er_chunk_ids=retry_er_chunk_ids,
        )

    async def delete_document_graph(self, file_path: str) -> dict[str, Any]:
        started = time.monotonic()
        normalized_file_path = index_storage_path(file_path)
        graph_error: Exception | None = None
        try:
            result = await self.repository.delete_document_graph(normalized_file_path)
        except Exception as exc:
            graph_error = exc
            result = {}

        try:
            if self._vector_enabled():
                await self._get_vector_store().delete_by_file_path(
                    workspace=self.workspace, file_path=normalized_file_path
                )
        except Exception as vector_error:
            if graph_error is not None:
                raise ExceptionGroup(
                    "Document graph and vector deletion both failed",
                    [graph_error, vector_error],
                ) from None
            raise
        if graph_error is not None:
            raise graph_error

        result["elapsed_sec"] = round(time.monotonic() - started, 3)
        return result

    async def delete_document(self, file_path: str) -> dict[str, Any]:
        return await self.delete_document_graph(file_path)

    async def clear_workspace(self, workspace: str | None = None) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        started = time.monotonic()
        neo = await self.repository.clear_workspace(target_workspace)
        if self._vector_enabled():
            await self._get_vector_store().clear_workspace(workspace=target_workspace)
        neo["elapsed_sec"] = round(time.monotonic() - started, 3)
        return neo

    async def clear_workspace_data(self, workspace: str | None = None) -> dict[str, Any]:
        return await self.clear_workspace(workspace=workspace)

    async def clear_fast_ingest_post_compile_injected(
        self, workspace: str | None = None
    ) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        started = time.monotonic()
        result = await self.repository.clear_fast_ingest_post_compile_injected(
            workspace=target_workspace
        )
        result["elapsed_sec"] = round(time.monotonic() - started, 3)
        return result

    async def show_fast_ingest_post_compile_summary(
        self, workspace: str | None = None
    ) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        return await self.repository.show_fast_ingest_post_compile_summary(
            workspace=target_workspace
        )

    async def list_lint_issues(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        return await self.repository.list_lint_issues(
            workspace=target_workspace,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def delete_lint_issue(
        self,
        *,
        workspace: str | None = None,
        issue_id: str,
    ) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        return await self.repository.delete_lint_issue(
            workspace=target_workspace,
            issue_id=issue_id,
        )

    async def delete_fast_ingest_post_compile_asset(
        self,
        *,
        workspace: str | None = None,
        asset_id: str,
    ) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        result = await self.repository.delete_fast_ingest_post_compile_asset(
            workspace=target_workspace,
            asset_id=asset_id,
        )
        if self._vector_enabled():
            await self._get_vector_store().delete_graph_vectors_by_asset_id(
                workspace=target_workspace,
                asset_id=asset_id,
            )
        return result

    async def lint_fast_ingest_post_compile_batch(
        self,
        *,
        workspace: str | None = None,
        batch_size: int | None = None,
        top_entities: int = 6,
        top_relations: int = 6,
        top_chunks: int = 4,
        max_hops: int = 2,
    ) -> dict[str, Any]:
        target_workspace = workspace or self.workspace
        batch_limit = max(1, int(batch_size or getattr(self.config, "lint_batch_size", 32) or 32))
        batch_id = f"lint-{_now_ts()}"
        started = time.monotonic()

        if self.repository.driver is None:
            raise RuntimeError("Repository not initialized")

        async with self.repository.driver.session(database=self.repository.database) as session:
            entity_result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE coalesce(n.status, '') = 'PENDING_LINT'
                  AND coalesce(n.post_compile_source, '') = $post_compile_source
                RETURN n.node_id AS node_id,
                       n.entity_name AS entity_name,
                       n.file_path AS file_path,
                       n.entity_type AS entity_type,
                       n.entity_description AS entity_description,
                       coalesce(n.aliases, []) AS aliases,
                       coalesce(n.alias_keys, []) AS alias_keys,
                       coalesce(n.chunk_ids, []) AS chunk_ids
                ORDER BY coalesce(n.created_at, 0) ASC
                LIMIT $limit
                """,
                workspace=target_workspace,
                post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                limit=batch_limit,
            )
            entity_rows: list[dict[str, Any]] = []
            async for record in entity_result:
                entity_rows.append(record.data())

            relation_limit = max(0, batch_limit - len(entity_rows))
            relation_rows: list[dict[str, Any]] = []
            if relation_limit:
                relation_result = await session.run(
                    f"""
                    MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                          -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]->
                          (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                    WHERE coalesce(r.status, '') = 'PENDING_LINT'
                      AND coalesce(r.post_compile_source, '') = $post_compile_source
                    RETURN r.relation_id AS relation_id,
                           r.file_path AS file_path,
                           src.node_id AS source_node_id,
                           src.entity_name AS source_entity,
                           tgt.node_id AS target_node_id,
                           tgt.entity_name AS target_entity,
                           coalesce(r.relationship_description, '') AS relationship_description,
                           coalesce(r.situation, '') AS situation,
                           coalesce(r.relation_keywords, []) AS relation_keywords,
                           coalesce(r.chunk_ids, []) AS chunk_ids
                    ORDER BY coalesce(r.created_at, 0) ASC
                    LIMIT $limit
                    """,
                    workspace=target_workspace,
                    post_compile_source=FAST_INGEST_POST_COMPILE_SOURCE,
                    limit=relation_limit,
                )
                async for record in relation_result:
                    relation_rows.append(record.data())

        if not entity_rows and not relation_rows:
            summary = await self.repository.show_fast_ingest_post_compile_summary(
                workspace=target_workspace
            )
            summary["batch_id"] = batch_id
            summary["elapsed_sec"] = round(time.monotonic() - started, 3)
            summary["reviewed_entities"] = 0
            summary["reviewed_relations"] = 0
            summary["issues_created"] = 0
            return summary

        reviewed_entities = 0
        reviewed_relations = 0
        issues_created = 0
        issue_rows: list[dict[str, Any]] = []

        async def _set_status(*, item_type: str, item_id: str, file_path: str, status: str) -> None:
            async with self.repository.driver.session(database=self.repository.database) as session:
                if item_type == "entity":
                    await session.run(
                        f"""
                        MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $item_id}})
                        WHERE n.file_path = $file_path
                        SET n.status = $status,
                            n.lint_batch_id = $batch_id,
                            n.lint_updated_at = timestamp()
                        """,
                        workspace=target_workspace,
                        file_path=file_path,
                        item_id=item_id,
                        status=status,
                        batch_id=batch_id,
                    )
                else:
                    await session.run(
                        f"""
                        MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, file_path: $file_path}})
                              -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace, file_path: $file_path, relation_id: $item_id}}]->
                              (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, file_path: $file_path}})
                        SET r.status = $status,
                            r.lint_batch_id = $batch_id,
                            r.lint_updated_at = timestamp()
                        """,
                        workspace=target_workspace,
                        file_path=file_path,
                        item_id=item_id,
                        status=status,
                        batch_id=batch_id,
                    )

        async def _record_issue(
            *,
            item_type: str,
            item_id: str,
            file_path: str,
            title: str,
            detail: str,
            issue_type: str,
            evidence: dict[str, Any],
        ) -> str:
            issue_id = _md5_id("lint-", target_workspace, item_type, item_id, batch_id)
            issue_payload = {
                "workspace": target_workspace,
                "issue_id": issue_id,
                "file_path": file_path,
                "item_type": item_type,
                "item_id": item_id,
                "issue_type": issue_type,
                "title": _truncate_text(title, 120),
                "detail": _truncate_text(detail, 1200),
                "evidence": evidence,
                "batch_id": batch_id,
                "post_compile_source": FAST_INGEST_POST_COMPILE_SOURCE,
            }
            issue_rows.append(issue_payload)
            async with self.repository.driver.session(database=self.repository.database) as session:
                await session.run(
                    f"""
                    MERGE (i:{LINT_ISSUE_LABEL} {{workspace: $workspace, issue_id: $issue_id}})
                    SET i.file_path = $file_path,
                        i.item_type = $item_type,
                        i.item_id = $item_id,
                        i.issue_type = $issue_type,
                        i.title = $title,
                        i.detail = $detail,
                        i.evidence = $evidence,
                        i.batch_id = $batch_id,
                        i.post_compile_source = $post_compile_source,
                        i.status = 'OPEN',
                        i.updated_at = timestamp(),
                        i.created_at = coalesce(i.created_at, timestamp())
                    """,
                    **issue_payload,
                )
            return issue_id

        async def _review_payload(
            *,
            item_type: str,
            target_name: str,
            target_description: str,
            context: dict[str, Any],
        ) -> dict[str, Any]:
            prompt = json.dumps(
                {
                    "task": "Review one post-compile object for linting.",
                    "decision_rules": [
                        "Return JSON only.",
                        "decision must be one of VERIFIED, CONFLICT_HOLD, NEEDS_MORE_CONTEXT.",
                        "If the object is missing essential semantics, choose CONFLICT_HOLD.",
                        "If evidence is sufficient but wording is incomplete, choose NEEDS_MORE_CONTEXT.",
                        "Prefer VERIFIED only when the object is grounded and consistent with the impact zone.",
                    ],
                    "target": {
                        "type": item_type,
                        "name": target_name,
                        "description": target_description,
                    },
                    "impact_zone": context,
                    "output_schema": {
                        "decision": "VERIFIED|CONFLICT_HOLD|NEEDS_MORE_CONTEXT",
                        "issue_type": "conflict|incomplete|none",
                        "issue_title": "short title",
                        "issue_detail": "short detail",
                        "refined_description": "optional short text",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            raw = await self._get_extractor().complete_text(
                prompt,
                system_prompt=(
                    "You are a strict knowledge-graph lint reviewer. "
                    "Return a compact JSON object only."
                ),
                max_tokens=512,
            )
            parsed = SingleLayerKGExtractor._parse_json(raw)
            if not isinstance(parsed, dict):
                parsed = {}
            return parsed

        for row in entity_rows:
            node_id = str(row.get("node_id") or "").strip()
            file_path = str(row.get("file_path") or "").strip()
            if not node_id:
                continue
            reviewed_entities += 1
            await _set_status(
                item_type="entity", item_id=node_id, file_path=file_path, status="LINTING"
            )
            context = await self.query_workspace_graph(
                query=f"{row.get('entity_name') or ''} {row.get('entity_type') or ''} {row.get('entity_description') or ''}",
                top_entities=top_entities,
                top_relations=top_relations,
                top_chunks=top_chunks,
                use_llm_keywords=False,
            )
            context["max_hops"] = max_hops
            context["lint_source"] = "entity"
            context["target"] = {
                "node_id": node_id,
                "file_path": file_path,
                "entity_name": row.get("entity_name") or "",
                "entity_type": row.get("entity_type") or "",
                "entity_description": row.get("entity_description") or "",
                "aliases": row.get("aliases") or [],
                "alias_keys": row.get("alias_keys") or [],
                "chunk_ids": row.get("chunk_ids") or [],
            }
            decision = await _review_payload(
                item_type="entity",
                target_name=str(row.get("entity_name") or ""),
                target_description=str(row.get("entity_description") or ""),
                context=context,
            )
            verdict = str(decision.get("decision") or "").upper()
            issue_type = str(decision.get("issue_type") or "").lower()
            if not verdict:
                verdict = (
                    "VERIFIED"
                    if len(str(row.get("entity_description") or "").strip()) >= 8
                    else "CONFLICT_HOLD"
                )
            if verdict == "VERIFIED":
                async with self.repository.driver.session(
                    database=self.repository.database
                ) as session:
                    await session.run(
                        f"""
                        MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $item_id}})
                        WHERE n.file_path = $file_path
                        SET n.status = 'VERIFIED',
                            n.entity_description = coalesce($refined_description, n.entity_description),
                            n.lint_batch_id = $batch_id,
                            n.lint_reviewed_at = timestamp()
                        """,
                        workspace=target_workspace,
                        file_path=file_path,
                        item_id=node_id,
                        refined_description=str(decision.get("refined_description") or "").strip()
                        or None,
                        batch_id=batch_id,
                    )
            else:
                issues_created += 1
                title = str(decision.get("issue_title") or "Lint issue").strip()
                detail = (
                    str(decision.get("issue_detail") or "").strip()
                    or "Lint review requires follow-up."
                )
                issue_id = await _record_issue(
                    item_type="entity",
                    item_id=node_id,
                    file_path=file_path,
                    title=title,
                    detail=detail,
                    issue_type=issue_type
                    or ("incomplete" if verdict == "NEEDS_MORE_CONTEXT" else "conflict"),
                    evidence={
                        "target": context.get("target"),
                        "impact_zone": {
                            "entities": context.get("entities") or [],
                            "relations": context.get("relations") or [],
                            "chunks": context.get("chunks") or [],
                        },
                    },
                )
                async with self.repository.driver.session(
                    database=self.repository.database
                ) as session:
                    await session.run(
                        f"""
                        MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $item_id}})
                        WHERE n.file_path = $file_path
                        SET n.status = 'CONFLICT_HOLD',
                            n.lint_issue_id = $issue_id,
                            n.lint_batch_id = $batch_id,
                            n.lint_reviewed_at = timestamp()
                        """,
                        workspace=target_workspace,
                        file_path=file_path,
                        item_id=node_id,
                        issue_id=issue_id,
                        batch_id=batch_id,
                    )

        for row in relation_rows:
            relation_id = str(row.get("relation_id") or "").strip()
            file_path = str(row.get("file_path") or "").strip()
            if not relation_id:
                continue
            reviewed_relations += 1
            await _set_status(
                item_type="relation",
                item_id=relation_id,
                file_path=file_path,
                status="LINTING",
            )
            context = await self.query_workspace_graph(
                query=f"{row.get('source_entity') or ''} {row.get('target_entity') or ''} {row.get('relationship_description') or ''}",
                top_entities=top_entities,
                top_relations=top_relations,
                top_chunks=top_chunks,
                use_llm_keywords=False,
            )
            context["max_hops"] = max_hops
            context["lint_source"] = "relation"
            context["target"] = {
                "relation_id": relation_id,
                "file_path": file_path,
                "source_node_id": row.get("source_node_id") or "",
                "target_node_id": row.get("target_node_id") or "",
                "source_entity": row.get("source_entity") or "",
                "target_entity": row.get("target_entity") or "",
                "relationship_description": row.get("relationship_description") or "",
                "situation": row.get("situation") or "",
                "relation_keywords": row.get("relation_keywords") or [],
                "chunk_ids": row.get("chunk_ids") or [],
            }
            decision = await _review_payload(
                item_type="relation",
                target_name=f"{row.get('source_entity') or ''} -> {row.get('target_entity') or ''}",
                target_description=str(row.get("relationship_description") or ""),
                context=context,
            )
            verdict = str(decision.get("decision") or "").upper()
            issue_type = str(decision.get("issue_type") or "").lower()
            if not verdict:
                verdict = (
                    "VERIFIED"
                    if len(str(row.get("relationship_description") or "").strip()) >= 8
                    else "CONFLICT_HOLD"
                )
            if verdict == "VERIFIED":
                async with self.repository.driver.session(
                    database=self.repository.database
                ) as session:
                    await session.run(
                        f"""
                        MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $source_node_id, file_path: $file_path}})
                              -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace, relation_id: $relation_id, file_path: $file_path}}]->
                              (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $target_node_id, file_path: $file_path}})
                        SET r.status = 'VERIFIED',
                            r.relationship_description = coalesce($refined_description, r.relationship_description),
                            r.lint_batch_id = $batch_id,
                            r.lint_reviewed_at = timestamp()
                        """,
                        workspace=target_workspace,
                        file_path=file_path,
                        source_node_id=str(row.get("source_node_id") or ""),
                        target_node_id=str(row.get("target_node_id") or ""),
                        relation_id=relation_id,
                        refined_description=str(decision.get("refined_description") or "").strip()
                        or None,
                        batch_id=batch_id,
                    )
            else:
                issues_created += 1
                title = str(decision.get("issue_title") or "Lint issue").strip()
                detail = (
                    str(decision.get("issue_detail") or "").strip()
                    or "Lint review requires follow-up."
                )
                issue_id = await _record_issue(
                    item_type="relation",
                    item_id=relation_id,
                    file_path=file_path,
                    title=title,
                    detail=detail,
                    issue_type=issue_type
                    or ("incomplete" if verdict == "NEEDS_MORE_CONTEXT" else "conflict"),
                    evidence={
                        "target": context.get("target"),
                        "impact_zone": {
                            "entities": context.get("entities") or [],
                            "relations": context.get("relations") or [],
                            "chunks": context.get("chunks") or [],
                        },
                    },
                )
                async with self.repository.driver.session(
                    database=self.repository.database
                ) as session:
                    await session.run(
                        f"""
                        MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $source_node_id, file_path: $file_path}})
                              -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace, relation_id: $relation_id, file_path: $file_path}}]->
                              (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace, node_id: $target_node_id, file_path: $file_path}})
                        SET r.status = 'CONFLICT_HOLD',
                            r.lint_issue_id = $issue_id,
                            r.lint_batch_id = $batch_id,
                            r.lint_reviewed_at = timestamp()
                        """,
                        workspace=target_workspace,
                        file_path=file_path,
                        source_node_id=str(row.get("source_node_id") or ""),
                        target_node_id=str(row.get("target_node_id") or ""),
                        relation_id=relation_id,
                        issue_id=issue_id,
                        batch_id=batch_id,
                    )

        summary = await self.repository.show_fast_ingest_post_compile_summary(
            workspace=target_workspace
        )
        summary.update(
            {
                "batch_id": batch_id,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "reviewed_entities": reviewed_entities,
                "reviewed_relations": reviewed_relations,
                "issues_created": issues_created,
                "lint_issues": issue_rows,
            }
        )
        return summary

    async def clear_test_graphs(self, workspace_prefix: str = "tlkg_") -> dict[str, Any]:
        started = time.monotonic()
        neo = await self.repository.clear_workspaces_by_prefix(workspace_prefix)
        if self._vector_enabled():
            await self._get_vector_store().clear_workspaces_by_prefix(workspace_prefix)
        neo["elapsed_sec"] = round(time.monotonic() - started, 3)
        return neo

    async def show_document_graph(self, file_path: str) -> dict[str, Any]:
        return await self.repository.show_document_graph(index_storage_path(file_path))

    async def get_document_graph(self, file_path: str) -> dict[str, Any]:
        return await self.show_document_graph(file_path)

    async def show_synonyms(
        self, *, file_path: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        normalized_file_path = None
        if file_path:
            normalized_file_path = index_storage_path(file_path)
        return await self.repository.show_synonyms(file_path=normalized_file_path, limit=limit)

    async def show_graph_summary(self) -> dict[str, Any]:
        return await self.repository.show_graph_summary()

    async def get_workspace_summary(self) -> dict[str, Any]:
        return await self.show_graph_summary()

    async def upsert_manual_chunk(
        self,
        *,
        chunk_id: str,
        file_path: str,
        content: str,
        order: int,
        metadata: dict[str, Any] | None = None,
        segment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self._vector_enabled():
            raise RuntimeError("manual chunk operations require kg_vector_enabled")
        normalized_file_path = index_storage_path(file_path)
        normalized_content = str(content or "")
        embedding = (await self._get_embedding_client().embed_texts([normalized_content]))[0]
        row_metadata = {
            "chunk_id": chunk_id,
            "file_path": normalized_file_path,
            "order": int(order),
            "text": normalized_content,
            "tokens": len(_build_chunk_tokenizer(self.config.llm_model).encode(normalized_content)),
            "segment_ids": list(segment_ids or []),
            "metadata": dict(metadata or {}),
        }
        await self._get_vector_store().upsert_chunks(
            [
                {
                    "workspace": self.workspace,
                    "item_id": chunk_id,
                    "file_path": normalized_file_path,
                    "content": normalized_content,
                    "embedding": embedding,
                    "metadata": row_metadata,
                }
            ]
        )
        return row_metadata

    async def upsert_fast_ingest_post_compile_vectors(
        self, edges: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not self._vector_enabled():
            return {}

        entity_nodes: dict[str, GraphEntityNode] = {}
        entity_doc_ids: dict[str, str] = {}
        relation_edges: list[GraphRelationEdge] = []
        relation_doc_ids: dict[str, str] = {}
        for edge in edges:
            source_entity = str(edge.get("source_entity") or "").strip()
            target_entity = str(edge.get("target_entity") or "").strip()
            source_node_id = str(edge.get("source_canonical_id") or "").strip()
            target_node_id = str(edge.get("target_canonical_id") or "").strip()
            relation_id = str(edge.get("edge_id") or "").strip()
            doc_id = str(edge.get("doc_id") or "").strip()
            file_path = str(edge.get("source_file_path") or edge.get("file_path") or "").strip()
            if not all(
                [
                    source_entity,
                    target_entity,
                    source_node_id,
                    target_node_id,
                    relation_id,
                    file_path,
                ]
            ):
                continue
            chunk_ids = _dedupe_keep_order(
                ref.split(":", 1)[1]
                for ref in edge.get("source_refs") or []
                if ":" in str(ref) and str(ref).split(":", 1)[1].strip()
            )
            for node_id, entity_name in (
                (source_node_id, source_entity),
                (target_node_id, target_entity),
            ):
                node = entity_nodes.get(node_id)
                if node is None:
                    entity_nodes[node_id] = GraphEntityNode(
                        node_id=node_id,
                        entity_name=entity_name,
                        entity_type="Concept",
                        entity_description=entity_name,
                        file_path=file_path,
                        status="PENDING_LINT",
                        chunk_ids=chunk_ids,
                        normalized_name=_normalize_entity_lookup_key(entity_name),
                        alias_keys=[_normalize_entity_lookup_key(entity_name)],
                    )
                    entity_doc_ids[node_id] = doc_id
                else:
                    node.chunk_ids = _dedupe_keep_order([*node.chunk_ids, *chunk_ids])
            relation_description = str(
                edge.get("relation_description") or edge.get("relation_type") or ""
            ).strip()
            relation_edges.append(
                GraphRelationEdge(
                    relation_id=relation_id,
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relationship_description=relation_description,
                    situation=relation_description,
                    relation_keywords=_derive_relation_keywords(
                        source_entity,
                        target_entity,
                        str(edge.get("relation_type") or relation_description),
                        relation_description,
                        limit=6,
                    ),
                    synonym=False,
                    file_path=file_path,
                    status="PENDING_LINT",
                    chunk_ids=chunk_ids,
                )
            )

        relation_doc_ids = {
            str(edge.get("edge_id") or ""): str(edge.get("doc_id") or "")
            for edge in edges
            if str(edge.get("edge_id") or "").strip()
        }
        graph = DocumentGraph(
            workspace=self.workspace,
            file_path=FAST_INGEST_POST_COMPILE_FILE_PATH,
            source_uri="fast_ingest://post_compile",
            modality="fast_ingest",
            title="fast_ingest_post_compile",
            asset={},
            segments=[],
            chunks=[],
            extracted_entities=[],
            extracted_relations=[],
            entity_nodes=list(entity_nodes.values()),
            relation_edges=relation_edges,
        )
        entity_rows, relation_rows = await self._build_graph_vector_rows(graph)
        for row in entity_rows:
            row["metadata"]["doc_id"] = entity_doc_ids.get(str(row["item_id"]), "")
        for row in relation_rows:
            row["metadata"]["doc_id"] = relation_doc_ids.get(str(row["item_id"]), "")
        store = self._get_vector_store()
        await store.upsert_entities(entity_rows)
        await store.upsert_relations(relation_rows)
        return {
            "entity_vectors": len(entity_rows),
            "relation_vectors": len(relation_rows),
            "schema": self.config.kg_vector_schema,
        }

    async def delete_manual_chunk(self, *, chunk_id: str) -> None:
        if not self._vector_enabled():
            raise RuntimeError("manual chunk operations require kg_vector_enabled")
        await self._get_vector_store().delete_chunk(workspace=self.workspace, chunk_id=chunk_id)

    async def export_precompiled_package(
        self,
        *,
        workspaces: list[str] | None = None,
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = await self.repository.export_graph_records(workspaces=workspaces)
        return build_precompile_export_package(
            entities=[item for item in (payload.get("entities") or []) if isinstance(item, dict)],
            relations=[item for item in (payload.get("relations") or []) if isinstance(item, dict)],
            company_replacements=company_replacements,
            alias_fullname_map=alias_fullname_map,
            workspaces=payload.get("workspaces") or [],
        )

    async def import_precompiled_package(
        self,
        *,
        package: dict[str, Any],
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        entity_rows, relation_rows = build_precompiled_import_payload(
            package=package,
            company_replacements=company_replacements,
            alias_fullname_map=alias_fullname_map,
            normalize_entity_lookup_key=_normalize_entity_lookup_key,
            dedupe_keep_order=_dedupe_keep_order,
            derive_relation_keywords=_derive_relation_keywords,
            md5_id=_md5_id,
        )

        entity_nodes = [GraphEntityNode(**row) for row in entity_rows]
        relation_edges = [GraphRelationEdge(**row) for row in relation_rows]

        graph = DocumentGraph(
            workspace=self.workspace,
            file_path=PRECOMPILED_FILE_PATH,
            source_uri="precompile://import",
            modality="precompiled_package",
            title="imported_precompiled_package",
            asset={},
            segments=[],
            chunks=[],
            extracted_entities=[],
            extracted_relations=[],
            entity_nodes=entity_nodes,
            relation_edges=relation_edges,
        )
        storage_summary = await self.repository.upsert_document_graph(graph)

        vector_summary: dict[str, Any] = {}
        if self._vector_enabled() and (graph.entity_nodes or graph.relation_edges):
            _, entity_rows, relation_rows = await self._build_vector_rows(graph)
            store = self._get_vector_store()
            await store.upsert_entities(entity_rows)
            await store.upsert_relations(relation_rows)
            vector_summary = {
                "entity_vectors": len(entity_rows),
                "relation_vectors": len(relation_rows),
                "schema": self.config.kg_vector_schema,
            }

        return {
            "workspace": self.workspace,
            "imported_entities": len(graph.entity_nodes),
            "imported_relations": len(graph.relation_edges),
            "storage": storage_summary,
            "vector_storage": vector_summary,
        }

    async def query_document_graph(
        self,
        *,
        file_path: str,
        query: str,
        artifacts_dir: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = True,
    ) -> dict[str, Any]:
        result = await self._get_query_engine().query_document(
            file_path=file_path,
            query=query,
            artifacts_dir=artifacts_dir,
            top_entities=top_entities,
            top_relations=top_relations,
            top_chunks=top_chunks,
            use_llm_keywords=use_llm_keywords,
        )
        return result.to_dict()

    async def query_document(
        self,
        *,
        file_path: str,
        query: str,
        artifacts_dir: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = True,
    ) -> dict[str, Any]:
        return await self.query_document_graph(
            file_path=file_path,
            query=query,
            artifacts_dir=artifacts_dir,
            top_entities=top_entities,
            top_relations=top_relations,
            top_chunks=top_chunks,
            use_llm_keywords=use_llm_keywords,
        )

    async def query_workspace_graph(
        self,
        *,
        query: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = False,
    ) -> dict[str, Any]:
        if not self._vector_enabled():
            result = await self._get_query_engine().query_workspace(
                query=query,
                top_entities=top_entities,
                top_relations=top_relations,
                top_chunks=top_chunks,
                use_llm_keywords=use_llm_keywords,
            )
            return result.to_dict()
        result = await self._get_mix_query_engine().query(
            query=query,
            top_entities=top_entities,
            top_relations=top_relations,
            top_chunks=top_chunks,
            use_llm_keywords=use_llm_keywords,
        )
        return result

    async def query_workspace(
        self,
        *,
        query: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = False,
    ) -> dict[str, Any]:
        return await self.query_workspace_graph(
            query=query,
            top_entities=top_entities,
            top_relations=top_relations,
            top_chunks=top_chunks,
            use_llm_keywords=use_llm_keywords,
        )

    async def query_cross_workspace(
        self,
        *,
        query: str,
        workspaces: list[str],
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
    ) -> dict[str, Any]:
        """Query shared vector indexes and workspace-local source support."""
        if not self._vector_enabled():
            raise NotImplementedError("Cross-workspace retrieval requires PostgreSQL vectors")
        return await self._get_mix_query_engine().query(
            query=query,
            workspaces=workspaces,
            top_entities=top_entities,
            top_relations=top_relations,
            top_chunks=top_chunks,
        )

    async def get_cross_workspace_chunks(
        self, *, chunk_keys: list[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        if not self._vector_enabled():
            raise NotImplementedError("Cross-workspace retrieval requires PostgreSQL vectors")
        return await self._get_vector_store().get_chunks_by_keys(chunk_keys=chunk_keys)

    async def _parse_and_chunk_document(
        self,
        *,
        input_path: str,
        mineru_dir: str | None,
        parser_params: dict[str, Any] | None,
    ) -> tuple[Asset, list[Segment], list[ChunkRecord]]:
        input_file = Path(input_path).expanduser().resolve()
        if not input_file.exists():
            raise FileNotFoundError(f"Input file does not exist: {input_file}")
        resolved_file_path = str(input_file)
        resolved_doc_id = input_file.stem
        cache_dir = Path(mineru_dir or self._default_mineru_dir())
        cache_dir.mkdir(parents=True, exist_ok=True)
        parser = MinerUParser()
        parse_params = dict(parser_params or {})
        parse_params.setdefault("method", self.config.mineru_method)
        parse_params.setdefault("lang", self.config.mineru_lang)
        parse_params.setdefault("device", self.config.mineru_device)
        parse_params.setdefault("backend", self.config.mineru_backend)
        parse_params.setdefault("formula", self.config.mineru_formula)
        parse_params.setdefault("table", self.config.mineru_table)
        asset, segments = await asyncio.to_thread(
            parser.parse_document,
            resolved_doc_id,
            input_file,
            cache_dir,
            parse_params,
        )
        chunks = await self._chunk_document(
            file_path=resolved_file_path,
            asset=asset,
            segments=segments,
        )
        return asset, segments, chunks

    async def _extract_entities_and_relations(
        self,
        *,
        asset_id: str,
        chunks: list[ChunkRecord],
        segments: list[Segment] | None = None,
        modality: str,
        title: str,
        extraction_mode: Literal["standard", "pre_compile", "fast_ingest"] = "standard",
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation], list[dict[str, str]]]:
        self._last_extraction_stats = {
            "mode": "llm",
            "structured_chunk_count": 0,
            "fallback_chunk_count": 0,
            "schema_hit_count": 0,
        }
        _emit_progress(
            f"SingleLayerKG: stage=entity_relation_extraction started asset={asset_id} "
            f"chunks={len(chunks)} llm_model={self.config.llm_model} "
            f"ER_extraction_max_async={self.config.ER_extraction_max_async}"
        )
        use_structured_path = (
            bool(getattr(self.config, "use_structured_video_er_extraction", False))
            and str(modality or "").lower() == "video"
        )

        structured_entities: list[ExtractedEntity] = []
        structured_relations: list[ExtractedRelation] = []
        fallback_chunks: list[ChunkRecord] = list(chunks)
        schema_hit_count = 0

        if use_structured_path:
            (
                structured_entities,
                structured_relations,
                fallback_chunks,
                schema_hit_count,
            ) = self._extract_structured_video_entities_relations(
                chunks=chunks,
                segments=segments or [],
            )

        llm_entities: list[ExtractedEntity] = []
        llm_relations: list[ExtractedRelation] = []
        failed_er_chunks: list[dict[str, str]] = []
        if fallback_chunks:
            (
                llm_entities,
                llm_relations,
                failed_er_chunks,
            ) = await self._extract_entities_relations_via_llm(
                chunks=fallback_chunks,
                modality=modality,
                title=title,
                extraction_mode=extraction_mode,
            )

        entities = [*structured_entities, *llm_entities]
        relations = [*structured_relations, *llm_relations]

        if use_structured_path and not fallback_chunks:
            mode = "structured"
        elif use_structured_path:
            mode = "fallback_llm"
        else:
            mode = "llm"

        self._last_extraction_stats = {
            "mode": mode,
            "structured_chunk_count": len(chunks) - len(fallback_chunks),
            "fallback_chunk_count": len(fallback_chunks),
            "schema_hit_count": schema_hit_count,
            "failed_er_chunk_count": len(failed_er_chunks),
        }

        mode_summary = [
            mode,
            len(chunks) - len(fallback_chunks),
            len(fallback_chunks),
        ]
        logger.info(
            "SingleLayerKG: entity/relation extraction mode=%s structured_chunks=%s fallback_chunks=%s",
            *mode_summary,
        )
        _emit_progress(
            f"SingleLayerKG: stage=entity_relation_extraction completed asset={asset_id} "
            f"chunks={len(chunks)} entities={len(entities)} relations={len(relations)}"
        )
        return entities, relations, failed_er_chunks

    async def _extract_entities_relations_via_llm(
        self,
        *,
        chunks: list[ChunkRecord],
        modality: str,
        title: str,
        extraction_mode: Literal["standard", "pre_compile", "fast_ingest"] = "standard",
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation], list[dict[str, str]]]:
        extractor = self._get_extractor(extraction_mode=extraction_mode)
        tasks = [
            asyncio.create_task(
                extractor.extract_chunk(
                    chunk=chunk,
                    modality=modality,
                    title=title,
                )
            )
            for chunk in chunks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        entities: list[ExtractedEntity] = []
        relations: list[ExtractedRelation] = []
        failed_er_chunks: list[dict[str, str]] = []
        for chunk, result in zip(chunks, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                logger.warning(
                    "ER extraction skipped failed chunk: chunk=%s error=%s",
                    chunk.chunk_id,
                    result,
                )
                failed_er_chunks.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "error": str(result),
                    }
                )
                continue
            chunk_entities, chunk_relations = result
            entities.extend(chunk_entities)
            relations.extend(chunk_relations)
        return entities, relations, failed_er_chunks

    def _extract_structured_video_entities_relations(
        self,
        *,
        chunks: list[ChunkRecord],
        segments: list[Segment],
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation], list[ChunkRecord], int]:
        segment_caption_data: dict[str, dict[str, Any]] = {}
        for segment in segments:
            if hasattr(segment, "model_dump"):
                segment_dict = segment.model_dump()
            elif isinstance(segment, dict):
                segment_dict = segment
            else:
                continue

            segment_id = str(segment_dict.get("segment_id") or "").strip()
            if not segment_id:
                continue
            extra = segment_dict.get("extra") if isinstance(segment_dict.get("extra"), dict) else {}
            caption_data = (
                extra.get("caption_data") if isinstance(extra.get("caption_data"), dict) else {}
            )
            if caption_data:
                segment_caption_data[segment_id] = caption_data

        entities: list[ExtractedEntity] = []
        relations: list[ExtractedRelation] = []
        fallback_chunks: list[ChunkRecord] = []
        schema_hit_count = 0

        for chunk in chunks:
            chunk_caption_data = [
                segment_caption_data.get(segment_id)
                for segment_id in chunk.segment_ids
                if isinstance(segment_caption_data.get(segment_id), dict)
            ]
            chunk_caption_data = [item for item in chunk_caption_data if item]
            if not chunk_caption_data:
                fallback_chunks.append(chunk)
                continue

            chunk_has_schema = False
            chunk_entities: dict[str, ExtractedEntity] = {}
            chunk_relations: dict[str, ExtractedRelation] = {}

            def ensure_entity(name: str, description: str) -> None:
                normalized_name = _normalize_entity_lookup_key(name)
                if not normalized_name:
                    return
                entity_key = f"concept::{normalized_name}"
                if entity_key in chunk_entities:
                    current = chunk_entities[entity_key]
                    current.entity_description = _merge_unique_text(
                        current.entity_description,
                        _truncate_text(description, 300),
                    )
                    return
                extraction_id = _md5_id("entx-", chunk.file_path, chunk.chunk_id, entity_key)
                chunk_entities[entity_key] = ExtractedEntity(
                    extraction_id=extraction_id,
                    file_path=chunk.file_path,
                    chunk_id=chunk.chunk_id,
                    entity_name=name,
                    entity_type="Concept",
                    entity_description=_truncate_text(description, 300),
                    aliases=[],
                    normalized_name=normalized_name,
                )

            for caption_data in chunk_caption_data:
                objects = (
                    caption_data.get("objects")
                    if isinstance(caption_data.get("objects"), list)
                    else []
                )
                actions = (
                    caption_data.get("actions")
                    if isinstance(caption_data.get("actions"), list)
                    else []
                )

                for obj in objects:
                    if not isinstance(obj, dict):
                        continue
                    obj_name = _normalize_text(str(obj.get("obj_name") or obj.get("name") or ""))
                    if not obj_name:
                        continue
                    chunk_has_schema = True
                    description = _normalize_text(str(obj.get("description") or ""))
                    if not description:
                        description = f"{obj_name}（来自结构化视频对象）"
                    ensure_entity(obj_name, description)

                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    subject = _normalize_text(str(action.get("subject") or ""))
                    target = _normalize_text(str(action.get("object") or ""))
                    verb = _normalize_text(str(action.get("verb") or ""))
                    caption = _normalize_text(str(action.get("caption") or ""))
                    if not subject or not target:
                        continue

                    chunk_has_schema = True
                    if subject:
                        ensure_entity(subject, f"{subject}（来自动作引用，待补充）")
                    if target:
                        ensure_entity(target, f"{target}（来自动作引用，待补充）")

                    relationship_description = caption or _normalize_text(
                        " ".join(part for part in (subject, verb, target) if part)
                    )
                    if not relationship_description:
                        continue
                    situation_parts: list[str] = []
                    begin_ts = (
                        chunk.metadata.get("begin_ts") if isinstance(chunk.metadata, dict) else None
                    )
                    end_ts = (
                        chunk.metadata.get("end_ts") if isinstance(chunk.metadata, dict) else None
                    )
                    if begin_ts is not None and end_ts is not None:
                        try:
                            situation_parts.append(
                                f"time_range={float(begin_ts):g}-{float(end_ts):g}s"
                            )
                        except Exception:
                            pass
                    situation = "；".join(situation_parts)

                    relation_key = "::".join(
                        [
                            _normalize_entity_lookup_key(subject),
                            _normalize_entity_lookup_key(target),
                        ]
                    )
                    if relation_key in chunk_relations:
                        current = chunk_relations[relation_key]
                        current.relationship_description = _merge_unique_text(
                            current.relationship_description,
                            relationship_description,
                        )
                        current.situation = _merge_unique_text(current.situation, situation)
                        current.relation_keywords = _dedupe_keep_order(
                            current.relation_keywords
                            + _derive_relation_keywords(
                                subject,
                                target,
                                relationship_description,
                                situation,
                                [verb] if verb else [],
                            )
                        )
                        continue
                    extraction_id = _md5_id("relx-", chunk.file_path, chunk.chunk_id, relation_key)
                    chunk_relations[relation_key] = ExtractedRelation(
                        extraction_id=extraction_id,
                        file_path=chunk.file_path,
                        chunk_id=chunk.chunk_id,
                        source_entity=subject,
                        target_entity=target,
                        relationship_description=relationship_description,
                        situation=situation,
                        relation_keywords=_derive_relation_keywords(
                            subject,
                            target,
                            relationship_description,
                            situation,
                            [verb] if verb else [],
                        ),
                    )

            if not chunk_has_schema:
                fallback_chunks.append(chunk)
                continue

            schema_hit_count += 1
            entities.extend(chunk_entities.values())
            relations.extend(chunk_relations.values())

        return entities, relations, fallback_chunks, schema_hit_count

    async def _chunk_document(
        self,
        *,
        file_path: str,
        asset: Asset,
        segments: list[Segment],
    ) -> list[ChunkRecord]:
        tokenizer = _build_chunk_tokenizer(self.config.llm_model)
        chunker = HyperChunker(
            merge_small_tables_threshold_tokens=self.config.merge_small_tables_threshold_tokens,
            respect_semantic_boundaries=self.config.respect_semantic_boundaries,
            enable_modal_replay=self.config.enable_modal_replay,
            enable_contextual_embeddings=self.config.enable_contextual_embeddings,
            contextual_llm_func=self._get_extractor().complete_text,
            contextual_max_tokens=self.config.contextual_max_tokens,
            contextual_window_chunks=self.config.contextual_window_chunks,
        )
        payload = {
            "asset": asset.model_dump(),
            "segments": [segment.model_dump() for segment in segments],
        }
        chunk_dicts = await chunker(
            tokenizer,
            json.dumps(payload, ensure_ascii=False),
            overlap_token_size=int(self.config.chunk_overlap_token_size or 0),
            max_token_size=int(self.config.chunk_token_size or 512),
        )
        chunks: list[ChunkRecord] = []
        for idx, chunk_data in enumerate(chunk_dicts):
            raw_content = str(chunk_data.get("content") or "")
            text = _extract_chunk_text(raw_content)
            order = int(chunk_data.get("order", chunk_data.get("chunk_order_index", idx)) or idx)
            segment_ids = [str(seg_id) for seg_id in chunk_data.get("segment_ids", []) or []]
            metadata = (
                chunk_data.get("metadata") if isinstance(chunk_data.get("metadata"), dict) else {}
            )
            chunk_id = _md5_id("chunk-", file_path, str(order), text[:512])
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    file_path=file_path,
                    order=order,
                    raw_content=raw_content,
                    text=text,
                    tokens=int(chunk_data.get("tokens") or 0),
                    segment_ids=segment_ids,
                    metadata=metadata,
                )
            )
        return chunks

    def _default_mineru_dir(self) -> str:
        if self.config.mineru_cache_dir:
            return self.config.mineru_cache_dir
        return str(Path(self.config.working_dir) / self.workspace / "mineru_cache")

    def _vector_enabled(self) -> bool:
        return bool(self.config.kg_vector_enabled)

    def _get_embedding_client(self) -> KGEmbeddingClient:
        if self.embedding_client is None:
            self.embedding_client = KGEmbeddingClient(
                model=self.config.embedding_model,
                base_url=self.config.embedding_base_url,
                api_key=self.config.embedding_api_key,
                provider=self.config.embedding_provider,
                timeout=self.config.embedding_timeout,
                batch_size=self.config.kg_vector_embedding_batch_size,
                max_concurrency=self.config.embedding_max_async,
            )
        return self.embedding_client

    def _get_vector_store(self) -> KGVectorStore:
        if self.vector_store is None:
            self.vector_store = KGVectorStore(
                workspace=self.workspace,
                schema=self.config.kg_vector_schema,
                vector_dim=int(self.config.embedding_dim),
                chunk_table=self.config.kg_vector_chunk_table,
                entity_table=self.config.kg_vector_entity_table,
                relation_table=self.config.kg_vector_relation_table,
            )
        return self.vector_store

    def _build_entity_merge_summary_helper(self) -> EntityMergeSummaryHelper:
        return EntityMergeSummaryHelper(
            llm_func=self._get_extractor().complete_text
            if self.config.entity_merge_summary_enabled
            else None,
            enabled=self.config.entity_merge_summary_enabled,
            trigger_fragments=self.config.entity_merge_summary_trigger_fragments,
            trigger_tokens=self.config.entity_merge_summary_trigger_tokens,
            context_size=self.config.entity_merge_summary_context_size,
            max_output_tokens=self.config.entity_merge_summary_max_output_tokens,
        )

    def _configure_postgres_env(self) -> None:
        addon_params = self.config.addon_params or {}
        env_mapping = {
            "POSTGRES_USER": addon_params.get("postgres_user"),
            "POSTGRES_PASSWORD": addon_params.get("postgres_password"),
            "POSTGRES_HOST": addon_params.get("postgres_host"),
            "POSTGRES_PORT": addon_params.get("postgres_port"),
            "POSTGRES_DATABASE": addon_params.get("postgres_database"),
            "POSTGRES_MAX_CONNECTIONS": addon_params.get("postgres_max_connections"),
            "POSTGRES_WORKSPACE": self.workspace,
            "POSTGRES_ENABLE_VECTOR": "true",
        }
        for env_name, env_value in env_mapping.items():
            if env_value in (None, ""):
                continue
            os.environ[env_name] = str(env_value)

    async def _build_chunk_vector_rows(self, graph: DocumentGraph) -> list[dict[str, Any]]:
        chunk_embeddings = await self._embed_chunks(graph)
        return [
            {
                "workspace": graph.workspace,
                "item_id": chunk.chunk_id,
                "file_path": chunk.file_path,
                "content": chunk.text,
                "embedding": embedding,
                "metadata": {
                    "chunk_id": chunk.chunk_id,
                    "file_path": chunk.file_path,
                    "order": chunk.order,
                    "text": chunk.text,
                    "tokens": chunk.tokens,
                    "segment_ids": chunk.segment_ids,
                    "metadata": chunk.metadata,
                },
            }
            for chunk, embedding in zip(graph.chunks, chunk_embeddings, strict=True)
        ]

    async def _embed_chunks(self, graph: DocumentGraph) -> list[list[float]]:
        if not graph.chunks:
            return []
        video_chunks = [
            chunk
            for chunk in graph.chunks
            if chunk.metadata.get("chunk_type") == "video" or graph.modality == "video"
        ]
        video_ids = {chunk.chunk_id for chunk in video_chunks}
        text_chunks = [chunk for chunk in graph.chunks if chunk.chunk_id not in video_ids]
        vectors: dict[str, list[float]] = {}
        if video_chunks:
            segments = {
                str(segment["segment_id"]): segment
                for segment in graph.segments
                if segment.get("type") == "video_clip"
            }
            linked_segments: set[str] = set()
            associations: list[tuple[ChunkRecord, str]] = []
            for chunk in video_chunks:
                matching = [sid for sid in chunk.segment_ids if sid in segments]
                if len(matching) != 1 or matching[0] in linked_segments:
                    raise ValueError(
                        f"Video chunk must map to one unique segment: {chunk.chunk_id}"
                    )
                linked_segments.add(matching[0])
                associations.append((chunk, matching[0]))
            paths: list[str] = []
            texts: list[str] = []
            for chunk, segment_id in associations:
                segment = segments[segment_id]
                location = segment.get("location") or {}
                if location.get("begin_ts") is None or location.get("end_ts") is None:
                    raise ValueError(f"Video segment is missing its time range: {segment_id}")
                paths.append(
                    await asyncio.to_thread(
                        resolve_segment_video_path,
                        global_config={"working_dir": self.config.working_dir},
                        workspace=graph.workspace,
                        video_name=graph.title,
                        segment_id=segment_id,
                        video_output_format=self.config.video_segment_output_format,
                        metadata={
                            "asset_id": graph.asset.get("doc_id"),
                            "file_path": graph.file_path,
                            **location,
                            "caption_metadata": (segment.get("extra") or {}).get(
                                "caption_metadata"
                            ),
                        },
                    )
                )
                texts.append(build_video_embedding_text(segment))
            if self.multimodal_embedder is None:
                from .config_loader import build_default_multimodal_embedder

                self.multimodal_embedder = build_default_multimodal_embedder(self.config)
            embeddings = await encode_video_segments(
                paths,
                self.multimodal_embedder,
                texts,
                max_concurrency=self.config.embedding_max_async,
            )
            vectors.update(
                (chunk.chunk_id, embedding.tolist())
                for chunk, embedding in zip(video_chunks, embeddings, strict=True)
            )
        if text_chunks:
            embeddings = await self._get_embedding_client().embed_texts(
                [chunk.text for chunk in text_chunks]
            )
            vectors.update(
                (chunk.chunk_id, embedding)
                for chunk, embedding in zip(text_chunks, embeddings, strict=True)
            )
        ordered = [vectors[chunk.chunk_id] for chunk in graph.chunks]
        if any(
            len(vector) != self.config.embedding_dim
            or any(not math.isfinite(value) for value in vector)
            for vector in ordered
        ):
            raise ValueError("Chunk embedding dimension or values do not match the KG vector space")
        return ordered

    async def _build_graph_vector_rows(
        self, graph: DocumentGraph
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        embedding_client = self._get_embedding_client()
        entity_embeddings = await embedding_client.embed_texts(
            [self._entity_vector_text(node) for node in graph.entity_nodes]
        )
        relation_embeddings = await embedding_client.embed_texts(
            [self._relation_vector_text(edge) for edge in graph.relation_edges]
        )
        entity_rows = [
            {
                "workspace": graph.workspace,
                "item_id": node.node_id,
                "file_path": node.file_path,
                "content": self._entity_vector_text(node),
                "embedding": embedding,
                "metadata": {
                    "node_id": node.node_id,
                    "file_path": node.file_path,
                    "entity_name": node.entity_name,
                    "entity_type": node.entity_type,
                    "entity_description": node.entity_description,
                    "aliases": node.aliases,
                    "normalized_name": node.normalized_name,
                    "alias_keys": node.alias_keys,
                    "chunk_ids": node.chunk_ids,
                },
            }
            for node, embedding in zip(graph.entity_nodes, entity_embeddings, strict=True)
        ]
        relation_rows = [
            {
                "workspace": graph.workspace,
                "item_id": edge.relation_id,
                "file_path": edge.file_path,
                "content": self._relation_vector_text(edge),
                "embedding": embedding,
                "metadata": {
                    "relation_id": edge.relation_id,
                    "file_path": edge.file_path,
                    "source_entity": edge.source_entity,
                    "target_entity": edge.target_entity,
                    "relationship_description": edge.relationship_description,
                    "situation": edge.situation,
                    "relation_keywords": edge.relation_keywords,
                    "synonym": edge.synonym,
                    "chunk_ids": edge.chunk_ids,
                },
            }
            for edge, embedding in zip(graph.relation_edges, relation_embeddings, strict=True)
        ]
        return entity_rows, relation_rows

    async def _build_vector_rows(
        self,
        graph: DocumentGraph,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        chunk_rows = await self._build_chunk_vector_rows(graph)
        entity_rows, relation_rows = await self._build_graph_vector_rows(graph)
        return chunk_rows, entity_rows, relation_rows

    @staticmethod
    def _entity_vector_text(node: GraphEntityNode) -> str:
        alias_text = ", ".join(node.aliases)
        lines = [node.entity_name, f"type: {node.entity_type}"]
        if alias_text:
            lines.append(f"aliases: {alias_text}")
        if node.entity_description:
            lines.append(node.entity_description)
        return "\n".join(lines)

    @staticmethod
    def _relation_vector_text(edge: GraphRelationEdge) -> str:
        lines = [f"{edge.source_entity} -> {edge.target_entity}", edge.relationship_description]
        if edge.situation:
            lines.append(edge.situation)
        if edge.relation_keywords:
            lines.append(f"keywords: {', '.join(edge.relation_keywords)}")
        return "\n".join([line for line in lines if line])

    def _write_artifacts(self, graph: DocumentGraph, root_dir: Path) -> dict[str, str]:
        doc_dir = root_dir / Path(graph.file_path).stem
        doc_dir.mkdir(parents=True, exist_ok=True)
        graph_path = doc_dir / "document_graph.json"
        entities_path = doc_dir / "extracted_entities.json"
        relations_path = doc_dir / "extracted_relations.json"
        merged_nodes_path = doc_dir / "entity_nodes.json"
        edge_path = doc_dir / "relation_edges.json"
        graph_path.write_text(
            json.dumps(
                {
                    "summary": graph.to_summary(),
                    "asset": graph.asset,
                    "segments": graph.segments,
                    "chunks": [asdict(chunk) for chunk in graph.chunks],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        entities_path.write_text(
            json.dumps(
                [asdict(entity) for entity in graph.extracted_entities],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        relations_path.write_text(
            json.dumps(
                [asdict(relation) for relation in graph.extracted_relations],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        merged_nodes_path.write_text(
            json.dumps([asdict(node) for node in graph.entity_nodes], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        edge_path.write_text(
            json.dumps(
                [asdict(edge) for edge in graph.relation_edges], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        return {
            "document_graph": str(graph_path),
            "extracted_entities": str(entities_path),
            "extracted_relations": str(relations_path),
            "entity_nodes": str(merged_nodes_path),
            "relation_edges": str(edge_path),
        }


def build_parser_params_from_args(args: Any) -> dict[str, Any]:
    return _compact_dict(
        {
            "method": getattr(args, "method", None),
            "lang": getattr(args, "lang", None),
            "device": getattr(args, "device", None),
            "backend": getattr(args, "backend", None),
            "formula": getattr(args, "formula", None),
            "table": getattr(args, "table", None),
        }
    )
