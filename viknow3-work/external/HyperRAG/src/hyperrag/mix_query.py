from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from .kg_vector_store import KGEmbeddingClient, KGVectorStore

logger = logging.getLogger(__name__)


_SEARCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./+-]+|[\u4e00-\u9fff]+")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _normalize_key(value: str) -> str:
    normalized = _normalize_text(value).lower()
    if not normalized:
        return ""
    normalized = re.sub(r"[®™©]", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_search_text(value: str) -> str:
    return _normalize_text(value).lower()


def _dedupe_keep_order(values: Iterable[str]) -> List[str]:
    out: List[str] = []
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


def _tokenize_search_text(value: str) -> List[str]:
    tokens: List[str] = []
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
    return weight if needle_norm in haystack_norm else 0.0


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
    keywords: List[str],
    *,
    phrase_weight: float,
    token_weight: float,
) -> tuple[float, List[str]]:
    score = 0.0
    matched_terms: List[str] = []
    for keyword in _dedupe_keep_order(keywords):
        term_score = _phrase_score(haystack, keyword, phrase_weight)
        if term_score <= 0:
            term_score = _token_overlap_score(haystack, keyword, token_weight)
        if term_score <= 0:
            continue
        score += term_score
        matched_terms.append(keyword)
    return score, matched_terms


@dataclass
class MixQueryKeywords:
    query: str
    entity_keywords: List[str] = field(default_factory=list)
    topic_keywords: List[str] = field(default_factory=list)
    fallback_keywords: List[str] = field(default_factory=list)

    def all_keywords(self) -> List[str]:
        return _dedupe_keep_order(self.entity_keywords + self.topic_keywords + self.fallback_keywords)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "entity_keywords": self.entity_keywords,
            "topic_keywords": self.topic_keywords,
            "fallback_keywords": self.fallback_keywords,
        }


class SingleModeMixQueryEngine:
    def __init__(
        self,
        *,
        workspace: str,
        vector_store: KGVectorStore,
        embedding_client: KGEmbeddingClient,
        synonym_relation_writer: Optional[Callable[[List[Dict[str, Any]]], Awaitable[Dict[str, Any]]]] = None,
        query_synonym_expand_enabled: bool = True,
        query_synonym_expand_limit: int = 8,
        vector_entity_top_k: int = 20,
        vector_relation_top_k: int = 24,
        vector_chunk_top_k: int = 24,
    ) -> None:
        self.workspace = workspace
        self.vector_store = vector_store
        self.embedding_client = embedding_client
        self.synonym_relation_writer = synonym_relation_writer
        self.query_synonym_expand_enabled = query_synonym_expand_enabled
        self.query_synonym_expand_limit = query_synonym_expand_limit
        self.vector_entity_top_k = vector_entity_top_k
        self.vector_relation_top_k = vector_relation_top_k
        self.vector_chunk_top_k = vector_chunk_top_k
        self._pending_synonym_tasks: Set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        if not self._pending_synonym_tasks:
            return
        pending = list(self._pending_synonym_tasks)
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending_synonym_tasks.clear()

    async def query(
        self,
        *,
        query: str,
        top_entities: int = 10,
        top_relations: int = 12,
        top_chunks: int = 8,
        use_llm_keywords: bool = False,
        workspaces: Collection[str] | None = None,
    ) -> Dict[str, Any]:
        del use_llm_keywords
        keywords = self._build_keywords(query)
        all_keywords = keywords.all_keywords()
        query_text = (
            query
            if workspaces is not None
            else "\n".join(_dedupe_keep_order([query] + all_keywords))
        )
        if workspaces is not None and not workspaces:
            return {"entities": [], "relations": [], "chunks": []}
        query_vector = (await self.embedding_client.embed_texts([query_text]))[0]

        entity_limit = max(top_entities * 2, self.vector_entity_top_k)
        relation_limit = max(top_relations * 2, self.vector_relation_top_k)
        chunk_limit = max(top_chunks * 3, self.vector_chunk_top_k)

        async def retrieve(kind: str, top_k: int) -> list[dict[str, Any]]:
            if workspaces is not None:
                return await self.vector_store.query_cross_workspace(
                    kind=kind,
                    query_vector=query_vector,
                    workspaces=workspaces,
                    top_k=top_k,
                    min_similarity=-1.0,
                )
            return await getattr(self.vector_store, f"query_{kind}")(
                query_vector=query_vector,
                workspace=self.workspace,
                top_k=top_k,
                min_similarity=-1.0,
            )

        entity_rows, relation_rows, chunk_rows = await asyncio.gather(
            retrieve("entities", entity_limit),
            retrieve("relations", relation_limit),
            retrieve("chunks", chunk_limit),
        )
        if workspaces is not None:
            for row in entity_rows + relation_rows + chunk_rows:
                if not row.get("workspace") or row["workspace"] not in workspaces:
                    raise RuntimeError("Vector retrieval returned an invalid workspace")

        seed_entities = self._build_entity_hits(entity_rows, all_keywords)[:entity_limit]
        if workspaces is None:
            entity_hits, synonym_relations = await self._expand_entity_synonyms(
                seed_entities,
                all_keywords=all_keywords,
                limit=top_entities,
            )
        else:
            entity_hits = list(seed_entities)
            synonym_relations = []
            seeds_by_workspace: dict[str, list[dict[str, Any]]] = {}
            for seed in seed_entities[:top_entities]:
                seeds_by_workspace.setdefault(seed["workspace"], []).append(seed)
            for workspace, seeds in seeds_by_workspace.items():
                expanded, _ = await self._expand_entity_synonyms(
                    seeds,
                    all_keywords=all_keywords,
                    limit=top_entities,
                    workspace=workspace,
                )
                existing = {self._identity(item, "item_id") for item in entity_hits}
                entity_hits.extend(
                    item for item in expanded if self._identity(item, "item_id") not in existing
                )
            entity_hits.sort(key=lambda item: item["score"], reverse=True)
        entity_hits = entity_hits[:top_entities]
        self._schedule_synonym_writeback(synonym_relations)
        relation_hits = self._build_relation_hits(relation_rows, entity_hits=entity_hits, all_keywords=all_keywords)[:top_relations]
        chunk_hits = await self._build_chunk_hits(
            chunk_rows=chunk_rows,
            entity_hits=entity_hits,
            relation_hits=relation_hits,
            all_keywords=all_keywords,
            limit=top_chunks,
            cross_workspace=workspaces is not None,
        )
        langfuse_trace = self._build_langfuse_trace(
            chunk_hits=chunk_hits,
            vector_chunk_rows=chunk_rows,
            cross_workspace=workspaces is not None,
        )

        return {
            **(
                {"workspace": self.workspace}
                if workspaces is None
                else {"workspaces": list(workspaces)}
            ),
            "file_path": "",
            "query": query,
            "artifact_dir": "",
            "query_scope": "workspace" if workspaces is None else "cross_workspace",
            "retrieval_source": "kg_mix_single_mode",
            "keywords": keywords.to_dict(),
            "entities": entity_hits,
            "relations": relation_hits,
            "chunks": chunk_hits,
            "context_text": self._build_context_text(entity_hits, relation_hits, chunk_hits),
            "langfuse_trace": langfuse_trace,
            "stats": {
                "entity_candidates": len(entity_rows),
                "relation_candidates": len(relation_rows),
                "chunk_candidates": len(chunk_rows),
                "returned_entities": len(entity_hits),
                "returned_relations": len(relation_hits),
                "returned_chunks": len(chunk_hits),
                "query_synonym_expand_enabled": self.query_synonym_expand_enabled,
                "query_synonym_writeback_candidates": len(synonym_relations),
                "query_use_llm_keywords": False,
            },
        }

    @staticmethod
    def _format_langfuse_chunk_entries(
        chunks: list[dict[str, Any]], *, cross_workspace: bool = False
    ) -> list[str]:
        entries: List[str] = []
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id:
                continue
            prefix = f"{chunk['workspace']}|" if cross_workspace else ""
            entries.append(f"{prefix}{chunk_id}|{str(chunk.get('text') or '')}")
        return entries

    def _build_langfuse_trace(
        self,
        *,
        chunk_hits: List[Dict[str, Any]],
        vector_chunk_rows: List[Dict[str, Any]],
        cross_workspace: bool = False,
    ) -> Dict[str, Any]:
        vector_chunk_candidates = self._score_vector_chunks(vector_chunk_rows, [])
        vector_chunks_with_ids = self._format_langfuse_chunk_entries(
            vector_chunk_candidates, cross_workspace=cross_workspace
        )
        entity_chunks_with_ids = self._format_langfuse_chunk_entries(
            [chunk for chunk in chunk_hits if chunk.get("entity_hits")],
            cross_workspace=cross_workspace,
        )
        relation_chunks_with_ids = self._format_langfuse_chunk_entries(
            [chunk for chunk in chunk_hits if chunk.get("relation_hits")],
            cross_workspace=cross_workspace,
        )
        merged_chunks_with_ids = self._format_langfuse_chunk_entries(
            chunk_hits, cross_workspace=cross_workspace
        )
        chunk_ids = [
            self._identity(chunk, "chunk_id")
            if cross_workspace
            else str(chunk.get("chunk_id") or "")
            for chunk in chunk_hits
            if str(chunk.get("chunk_id") or "")
        ]
        return {
            "pre_topk_chunk_ids": chunk_ids,
            "output_data": {
                "vector_chunks": vector_chunks_with_ids,
                "entity_chunks": entity_chunks_with_ids,
                "relation_chunks": relation_chunks_with_ids,
                "merged_chunks": merged_chunks_with_ids,
            },
            "chunk_ids": chunk_ids,
            "chunks_with_ids": merged_chunks_with_ids,
            "bm25_chunk_ids": [],
            "bm25_chunks_with_ids": [],
            "combine_chunks_with_ids": merged_chunks_with_ids,
        }

    def _schedule_synonym_writeback(self, synonym_relations: List[Dict[str, Any]]) -> None:
        if not synonym_relations or self.synonym_relation_writer is None:
            return

        async def _writeback() -> None:
            try:
                await self.synonym_relation_writer(synonym_relations)
            except Exception:
                logger.exception("Query-time synonym writeback failed")

        task = asyncio.create_task(_writeback())
        self._pending_synonym_tasks.add(task)
        task.add_done_callback(self._pending_synonym_tasks.discard)

    def _build_keywords(self, query: str) -> MixQueryKeywords:
        fallback_keywords = _dedupe_keep_order([query] + _tokenize_search_text(query))
        return MixQueryKeywords(query=query, fallback_keywords=fallback_keywords)

    def _identity(self, item: dict[str, Any], id_field: str) -> tuple[str, str]:
        return str(item.get("workspace") or self.workspace), str(item.get(id_field) or "")

    def _build_entity_hits(self, rows: List[Dict[str, Any]], all_keywords: List[str]) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            item_id = str(row.get("item_id") or "")
            key = self._identity(row, "item_id")
            if not item_id or key in seen:
                continue
            seen.add(key)
            entity_name = str(metadata.get("entity_name") or row.get("content") or "")
            entity_type = str(metadata.get("entity_type") or "Concept")
            entity_description = str(metadata.get("entity_description") or row.get("content") or "")
            aliases = [str(alias) for alias in metadata.get("aliases") or [] if str(alias).strip()]
            lexical_score, matched_terms = _score_keyword_matches(
                "\n".join([entity_name, entity_description] + aliases),
                all_keywords,
                phrase_weight=5.0,
                token_weight=1.5,
            )
            vector_score = float(row.get("similarity") or 0.0)
            hits.append(
                {
                    "item_id": item_id,
                    "workspace": key[0],
                    "metadata": metadata,
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "entity_description": entity_description,
                    "file_path": str(row.get("file_path") or metadata.get("file_path") or ""),
                    "aliases": aliases,
                    "chunk_ids": [
                        str(chunk_id)
                        for chunk_id in metadata.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    "score": round(vector_score * 100 + lexical_score, 3),
                    "vector_score": round(vector_score, 6),
                    "normalized_name": str(
                        metadata.get("normalized_name") or _normalize_key(entity_name)
                    ),
                    "synonym_of": None,
                    "matched_terms": matched_terms,
                }
            )
        hits.sort(key=lambda item: item["score"], reverse=True)
        return hits

    def _build_relation_hits(
        self,
        rows: List[Dict[str, Any]],
        *,
        entity_hits: List[Dict[str, Any]],
        all_keywords: List[str],
    ) -> List[Dict[str, Any]]:
        entity_score_map = {
            (self._identity(item, "item_id")[0], item["entity_name"]): item["score"]
            for item in entity_hits
        }
        hits: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            relation_id = str(row.get("item_id") or metadata.get("relation_id") or "")
            workspace = self._identity(row, "item_id")[0]
            key = (workspace, relation_id)
            if not relation_id or key in seen:
                continue
            seen.add(key)
            source_entity = str(metadata.get("source_entity") or "")
            target_entity = str(metadata.get("target_entity") or "")
            relation_description = str(metadata.get("relationship_description") or row.get("content") or "")
            situation = str(metadata.get("situation") or "")
            relation_keywords = [
                str(keyword) for keyword in metadata.get("relation_keywords") or [] if str(keyword).strip()
            ]
            lexical_score, matched_terms = _score_keyword_matches(
                "\n".join([source_entity, target_entity, relation_description, situation] + relation_keywords),
                all_keywords,
                phrase_weight=4.0,
                token_weight=1.25,
            )
            support_score = (
                entity_score_map.get((workspace, source_entity), 0.0) * 0.15
                + entity_score_map.get((workspace, target_entity), 0.0) * 0.15
            )
            keyword_bonus = 1.0 * len(matched_terms)
            vector_score = float(row.get("similarity") or 0.0)
            hits.append(
                {
                    "relation_id": relation_id,
                    "workspace": workspace,
                    "metadata": metadata,
                    "source_entity": source_entity,
                    "target_entity": target_entity,
                    "relationship_description": relation_description,
                    "situation": situation,
                    "relation_keywords": relation_keywords,
                    "file_path": str(row.get("file_path") or metadata.get("file_path") or ""),
                    "chunk_ids": [
                        str(chunk_id)
                        for chunk_id in metadata.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    "score": round(
                        vector_score * 100 + lexical_score + support_score + keyword_bonus, 3
                    ),
                    "vector_score": round(vector_score, 6),
                    "matched_terms": matched_terms,
                }
            )
        hits.sort(key=lambda item: item["score"], reverse=True)
        return hits

    async def _expand_entity_synonyms(
        self,
        entity_hits: List[Dict[str, Any]],
        *,
        all_keywords: List[str],
        limit: int,
        workspace: str | None = None,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if not self.query_synonym_expand_enabled or not entity_hits:
            return entity_hits, []

        seed_entities = entity_hits[:limit]
        aggregated_keys: List[str] = []
        for entity in seed_entities:
            keys = _dedupe_keep_order([entity.get("normalized_name") or ""] + [_normalize_key(alias) for alias in entity.get("aliases") or []])
            if not keys:
                continue
            for key in keys:
                aggregated_keys.append(key)

        if not aggregated_keys:
            return entity_hits, []

        target_workspace = workspace or self.workspace
        synonym_rows = await self.vector_store.find_entity_synonyms(
            workspace=target_workspace,
            normalized_keys=_dedupe_keep_order(aggregated_keys),
            exclude_ids=[item["item_id"] for item in entity_hits],
            limit=max(limit, len(seed_entities) * max(1, self.query_synonym_expand_limit)),
        )

        expanded = list(entity_hits)
        seen = {self._identity(item, "item_id") for item in entity_hits}
        synonym_relations: Dict[str, Dict[str, Any]] = {}
        for row in synonym_rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            item_id = str(row.get("item_id") or "")
            if workspace is not None and not row.get("workspace"):
                raise RuntimeError("Synonym lookup did not return its workspace")
            key = (str(row.get("workspace") or target_workspace), item_id)
            if key[0] != target_workspace:
                raise RuntimeError("Synonym lookup returned a different workspace")
            if not item_id or key in seen:
                continue
            file_path = str(row.get("file_path") or metadata.get("file_path") or "")
            entity_name = str(metadata.get("entity_name") or row.get("content") or "")
            entity_description = str(metadata.get("entity_description") or row.get("content") or "")
            aliases = [str(alias) for alias in metadata.get("aliases") or [] if str(alias).strip()]
            row_keys = set(_dedupe_keep_order([
                str(metadata.get("normalized_name") or _normalize_key(entity_name)),
                *[_normalize_key(alias) for alias in aliases],
            ]))
            source_hit: Optional[Dict[str, Any]] = None
            for entity in seed_entities:
                entity_keys = set(_dedupe_keep_order([
                    entity.get("normalized_name") or "",
                    *[_normalize_key(alias) for alias in entity.get("aliases") or []],
                ]))
                if row_keys & entity_keys:
                    source_hit = entity
                    break
            if source_hit is None:
                continue
            if file_path == source_hit.get("file_path"):
                continue
            source_entity_name = str(source_hit.get("entity_name") or "")
            source_file_path = str(source_hit.get("file_path") or "")
            if source_entity_name and source_file_path and entity_name and file_path:
                relation_rows = self._build_synonym_relation_rows(
                    source_entity_name=source_entity_name,
                    source_file_path=source_file_path,
                    target_entity_name=entity_name,
                    target_file_path=file_path,
                    normalized_name=str(
                        metadata.get("normalized_name") or _normalize_key(entity_name)
                    ),
                    workspace=target_workspace,
                )
                for relation_row in relation_rows:
                    synonym_relations[relation_row["relation_id"]] = relation_row
            lexical_score, matched_terms = _score_keyword_matches(
                "\n".join([entity_name, entity_description] + aliases),
                all_keywords,
                phrase_weight=3.0,
                token_weight=1.0,
            )
            expanded.append(
                {
                    "item_id": item_id,
                    "workspace": target_workspace,
                    "metadata": metadata,
                    "entity_name": entity_name,
                    "entity_type": str(metadata.get("entity_type") or "Concept"),
                    "entity_description": entity_description,
                    "file_path": file_path,
                    "aliases": aliases,
                    "chunk_ids": [
                        str(chunk_id)
                        for chunk_id in metadata.get("chunk_ids") or []
                        if str(chunk_id).strip()
                    ],
                    "score": round(float(source_hit["score"]) * 0.8 + lexical_score, 3),
                    "vector_score": max(
                        0.0, round(float(source_hit.get("vector_score") or 0.0) * 0.8, 6)
                    ),
                    "normalized_name": str(
                        metadata.get("normalized_name") or _normalize_key(entity_name)
                    ),
                    "synonym_of": source_hit["entity_name"],
                    "matched_terms": matched_terms,
                }
            )
            seen.add(key)

        expanded.sort(key=lambda item: item["score"], reverse=True)
        return expanded, list(synonym_relations.values())

    def _build_synonym_relation_rows(
        self,
        *,
        source_entity_name: str,
        source_file_path: str,
        target_entity_name: str,
        target_file_path: str,
        normalized_name: str,
        workspace: str | None = None,
    ) -> List[Dict[str, Any]]:
        relation_description = "Cross-document synonym relation inferred from query-time retrieval"
        situation = "query_time_synonym_expansion"
        relation_keywords = _dedupe_keep_order(["synonym", normalized_name, source_entity_name, target_entity_name])

        def _build_row(left_entity: str, left_file: str, right_entity: str, right_file: str) -> Dict[str, Any]:
            relation_id = "syn-rel::" + "::".join(
                [
                    workspace or self.workspace,
                    _normalize_key(left_file),
                    _normalize_key(left_entity),
                    _normalize_key(right_file),
                    _normalize_key(right_entity),
                ]
            )
            return {
                "workspace": workspace or self.workspace,
                "relation_id": relation_id,
                "source_entity": left_entity,
                "source_file_path": left_file,
                "target_entity": right_entity,
                "target_file_path": right_file,
                "relationship_description": relation_description,
                "situation": situation,
                "relation_keywords": relation_keywords,
                "synonym": True,
                "file_path": left_file,
                "chunk_ids": [],
            }

        return [
            _build_row(source_entity_name, source_file_path, target_entity_name, target_file_path),
            _build_row(target_entity_name, target_file_path, source_entity_name, source_file_path),
        ]

    async def _build_chunk_hits(
        self,
        *,
        chunk_rows: List[Dict[str, Any]],
        entity_hits: List[Dict[str, Any]],
        relation_hits: List[Dict[str, Any]],
        all_keywords: List[str],
        limit: int,
        cross_workspace: bool = False,
    ) -> List[Dict[str, Any]]:
        vector_chunk_hits = self._score_vector_chunks(chunk_rows, all_keywords)

        chunk_row_map: dict[tuple[str, str], dict[str, Any]] = {
            self._identity(row, "item_id"): row
            for row in chunk_rows
            if str(row.get("item_id") or "")
        }
        entity_chunk_ids: list[tuple[str, str]] = []
        relation_chunk_ids: list[tuple[str, str]] = []
        entity_support: dict[tuple[str, str], list[str]] = {}
        relation_support: dict[tuple[str, str], list[str]] = {}

        for entity in entity_hits:
            for chunk_id in entity.get("chunk_ids") or []:
                normalized_chunk_id = str(chunk_id).strip()
                if not normalized_chunk_id:
                    continue
                key = (self._identity(entity, "item_id")[0], normalized_chunk_id)
                entity_chunk_ids.append(key)
                entity_support.setdefault(key, []).append(entity["entity_name"])

        for relation in relation_hits:
            relation_name = f"{relation['source_entity']} -> {relation['target_entity']}"
            for chunk_id in relation.get("chunk_ids") or []:
                normalized_chunk_id = str(chunk_id).strip()
                if not normalized_chunk_id:
                    continue
                key = (self._identity(relation, "relation_id")[0], normalized_chunk_id)
                relation_chunk_ids.append(key)
                relation_support.setdefault(key, []).append(relation_name)

        support_ids = list(dict.fromkeys(entity_chunk_ids + relation_chunk_ids))
        missing_ids = [chunk_id for chunk_id in support_ids if chunk_id not in chunk_row_map]
        if missing_ids:
            if cross_workspace:
                rows = await self.vector_store.get_chunks_by_keys(chunk_keys=missing_ids)
            else:
                rows = await self.vector_store.get_chunks_by_ids(
                    workspace=self.workspace, chunk_ids=[key[1] for key in missing_ids]
                )
            for row in rows:
                chunk_row_map[self._identity(row, "item_id")] = row
            if cross_workspace and set(missing_ids) - chunk_row_map.keys():
                raise RuntimeError("Graph source chunks are missing from the vector store")

        entity_supported_hits = self._score_supported_chunks(
            chunk_ids=list(dict.fromkeys(entity_chunk_ids)),
            chunk_row_map=chunk_row_map,
            entity_support=entity_support,
            relation_support={},
            all_keywords=all_keywords,
            source_label="entity",
        )
        relation_supported_hits = self._score_supported_chunks(
            chunk_ids=list(dict.fromkeys(relation_chunk_ids)),
            chunk_row_map=chunk_row_map,
            entity_support=entity_support,
            relation_support=relation_support,
            all_keywords=all_keywords,
            source_label="relation",
        )

        merged: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        max_len = max(len(vector_chunk_hits), len(entity_supported_hits), len(relation_supported_hits))
        for index in range(max_len):
            for source_hits in (vector_chunk_hits, entity_supported_hits, relation_supported_hits):
                if index >= len(source_hits):
                    continue
                hit = source_hits[index]
                chunk_id = self._identity(hit, "chunk_id")
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                merged.append(hit)
                if len(merged) >= limit:
                    return merged
        return merged[:limit]

    def _score_vector_chunks(self, rows: List[Dict[str, Any]], all_keywords: List[str]) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            chunk_id = str(row.get("item_id") or metadata.get("chunk_id") or "")
            if not chunk_id:
                continue
            text = str(metadata.get("text") or row.get("content") or "")
            lexical_score, matched_terms = _score_keyword_matches(
                text,
                all_keywords,
                phrase_weight=2.0,
                token_weight=0.75,
            )
            vector_score = float(row.get("similarity") or 0.0)
            hits.append(
                {
                    "chunk_id": chunk_id,
                    "workspace": self._identity(row, "item_id")[0],
                    "file_path": str(row.get("file_path") or metadata.get("file_path") or ""),
                    "order": int(metadata.get("order") or 0),
                    "text": text,
                    "score": round(vector_score * 100 + lexical_score, 3),
                    "vector_score": round(vector_score, 6),
                    "entity_hits": [],
                    "relation_hits": [],
                    "matched_terms": matched_terms,
                    "metadata": metadata,
                }
            )
        hits.sort(key=lambda item: (item["score"], -item["order"]), reverse=True)
        return hits

    def _score_supported_chunks(
        self,
        *,
        chunk_ids: list[tuple[str, str]],
        chunk_row_map: dict[tuple[str, str], dict[str, Any]],
        entity_support: dict[tuple[str, str], list[str]],
        relation_support: dict[tuple[str, str], list[str]],
        all_keywords: List[str],
        source_label: str,
    ) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for chunk_id in chunk_ids:
            row = chunk_row_map.get(chunk_id)
            if row is None:
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            text = str(metadata.get("text") or row.get("content") or "")
            lexical_score, matched_terms = _score_keyword_matches(
                text,
                all_keywords,
                phrase_weight=2.5,
                token_weight=0.9,
            )
            entity_names = _dedupe_keep_order(entity_support.get(chunk_id, []))
            relation_names = _dedupe_keep_order(relation_support.get(chunk_id, []))
            support_score = len(entity_names) * 4.0 + len(relation_names) * 2.5
            vector_score = float(row.get("similarity") or 0.0)
            source_bonus = 3.0 if source_label == "entity" else 2.0
            hits.append(
                {
                    "chunk_id": chunk_id[1],
                    "workspace": chunk_id[0],
                    "file_path": str(row.get("file_path") or metadata.get("file_path") or ""),
                    "order": int(metadata.get("order") or 0),
                    "text": text,
                    "score": round(
                        vector_score * 100 + lexical_score + support_score + source_bonus, 3
                    ),
                    "vector_score": round(vector_score, 6),
                    "entity_hits": entity_names,
                    "relation_hits": relation_names,
                    "matched_terms": matched_terms,
                    "metadata": metadata,
                }
            )
        hits.sort(key=lambda item: (item["score"], -item["order"]), reverse=True)
        return hits

    @staticmethod
    def _build_context_text(
        entity_hits: List[Dict[str, Any]],
        relation_hits: List[Dict[str, Any]],
        chunk_hits: List[Dict[str, Any]],
    ) -> str:
        sections = ["# Entities"]
        if entity_hits:
            for entity in entity_hits:
                sections.append(
                    f"- {entity['entity_name']} ({entity['entity_type']}): {entity['entity_description']}"
                )
        else:
            sections.append("- None")

        sections.append("\n# Relations")
        if relation_hits:
            for relation in relation_hits:
                relation_keywords = relation.get("relation_keywords") or []
                suffix = f" | keywords: {', '.join(relation_keywords)}" if relation_keywords else ""
                sections.append(
                    f"- {relation['source_entity']} -> {relation['target_entity']}: {relation['relationship_description']}"
                    f" | situation: {relation['situation']}{suffix}"
                )
        else:
            sections.append("- None")

        sections.append("\n# Chunks")
        if chunk_hits:
            for chunk in chunk_hits:
                sections.append(f"- [{chunk['chunk_id']}] {chunk['text']}")
        else:
            sections.append("- None")

        return "\n".join(sections)