from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

UNVERIFIED = "UNVERIFIED"
PENDING_LINT = "PENDING_LINT"
LINTING = "LINTING"
VERIFIED = "VERIFIED"
CONFLICT_HOLD = "CONFLICT_HOLD"
MIGRATING = "MIGRATING"


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _canonical_id(doc_id: str, entity_name: str) -> str:
    normalized = _normalize_text(entity_name)
    scoped = f"{_normalize_text(doc_id)}|{normalized}"
    digest = hashlib.sha1(scoped.encode("utf-8")).hexdigest()[:16]
    return f"canon-{digest}"


def _edge_id(doc_id: str, src_id: str, rel_type: str, tgt_id: str) -> str:
    payload = f"{_normalize_text(doc_id)}|{src_id}|{_normalize_text(rel_type)}|{tgt_id}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
    return f"edge-{digest}"


def _split_query_terms(query: str) -> list[str]:
    compact = _normalize_text(query)
    if not compact:
        return []
    return [term for term in compact.replace(",", " ").split(" ") if term]


@dataclass(frozen=True)
class FastIngestRelation:
    source_entity: str
    target_entity: str
    relation_type: str
    relation_description: str
    chunk_id: str
    file_path: str = ""


class FastIngestRuntime:
    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client

    @staticmethod
    def _entity_map_key(workspace: str) -> str:
        return f"hyperrag:fast_ingest:{workspace}:entity_map"

    @staticmethod
    def _edge_map_key(workspace: str) -> str:
        return f"hyperrag:fast_ingest:{workspace}:edges"

    @staticmethod
    def _doc_edges_key(workspace: str, doc_id: str) -> str:
        return f"hyperrag:fast_ingest:{workspace}:doc_edges:{doc_id}"

    @staticmethod
    def _workspaces_key() -> str:
        return "hyperrag:fast_ingest:workspaces"

    async def _execute_writes(
        self,
        *,
        hash_writes: list[tuple[str, str, str]],
        set_writes: list[tuple[str, str]],
    ) -> None:
        pipeline_factory = getattr(self.redis, "pipeline", None)
        if callable(pipeline_factory):
            pipeline = pipeline_factory()
            for key, field, value in hash_writes:
                pipeline.hset(key, field, value)
            for key, value in set_writes:
                pipeline.sadd(key, value)
            await pipeline.execute()
            return
        for key, field, value in hash_writes:
            await self.redis.hset(key, field, value)
        for key, value in set_writes:
            await self.redis.sadd(key, value)

    async def _batch_hget(self, *, key: str, fields: list[str]) -> dict[str, Any]:
        dedup_fields = [field for field in dict.fromkeys(fields) if field]
        if not dedup_fields:
            return {}
        pipeline_factory = getattr(self.redis, "pipeline", None)
        if callable(pipeline_factory):
            pipeline = pipeline_factory()
            for field in dedup_fields:
                pipeline.hget(key, field)
            values = await pipeline.execute()
            return {field: value for field, value in zip(dedup_fields, values, strict=False)}
        out: dict[str, Any] = {}
        for field in dedup_fields:
            out[field] = await self.redis.hget(key, field)
        return out

    async def _smembers(self, key: str) -> list[str]:
        reader = getattr(self.redis, "smembers", None)
        if not callable(reader):
            return []
        raw_items = await reader(key)
        out: list[str] = []
        for item in raw_items or []:
            if isinstance(item, bytes):
                out.append(item.decode("utf-8", errors="ignore"))
            else:
                out.append(str(item))
        return out

    @asynccontextmanager
    async def _workspace_lock(self, workspace: str):
        lock_factory = getattr(self.redis, "lock", None)
        if not callable(lock_factory):
            yield
            return
        lock = lock_factory(
            f"hyperrag:fast_ingest:{workspace}:write_lock",
            timeout=3600,
            blocking_timeout=5,
        )
        if not await lock.acquire():
            raise RuntimeError(f"fast ingest workspace lock unavailable: {workspace}")
        try:
            yield
        finally:
            await lock.release()

    async def ingest_relations(
        self,
        *,
        workspace: str,
        doc_id: str,
        relations: list[FastIngestRelation],
        compile_phase: str = "FAST_INGEST",
    ) -> dict[str, Any]:
        async with self._workspace_lock(workspace):
            return await self._ingest_relations(
                workspace=workspace,
                doc_id=doc_id,
                relations=relations,
                compile_phase=compile_phase,
            )

    async def _ingest_relations(
        self,
        *,
        workspace: str,
        doc_id: str,
        relations: list[FastIngestRelation],
        compile_phase: str = "FAST_INGEST",
    ) -> dict[str, Any]:
        entity_key = self._entity_map_key(workspace)
        edge_key = self._edge_map_key(workspace)
        created_edges = 0
        absorbed_edges = 0
        reused_entities = 0
        created_entities = 0
        normalized: list[dict[str, str]] = []
        entity_tokens: dict[str, str] = {}
        for relation in relations:
            src_name = str(relation.source_entity or "").strip()
            tgt_name = str(relation.target_entity or "").strip()
            rel_type = str(relation.relation_type or relation.relation_description or "").strip()
            if not src_name or not tgt_name or not rel_type:
                continue
            src_token = _normalize_text(src_name)
            tgt_token = _normalize_text(tgt_name)
            if not src_token or not tgt_token:
                continue
            src_scope = f"{doc_id}::{src_token}"
            tgt_scope = f"{doc_id}::{tgt_token}"
            entity_tokens.setdefault(src_scope, src_name)
            entity_tokens.setdefault(tgt_scope, tgt_name)
            normalized.append(
                {
                    "src_name": src_name,
                    "tgt_name": tgt_name,
                    "rel_type": rel_type,
                    "rel_desc": str(relation.relation_description or ""),
                    "src_scope": src_scope,
                    "tgt_scope": tgt_scope,
                    "source_ref": f"{doc_id}:{relation.chunk_id}",
                    "source_file_path": str(relation.file_path or "").strip(),
                }
            )

        if not normalized:
            return {
                "workspace": workspace,
                "doc_id": doc_id,
                "total_relations": 0,
                "created_edges": 0,
                "absorbed_edges": 0,
                "created_entities": 0,
                "reused_entities": 0,
                "duplicate_absorb_rate": 0.0,
            }

        await self.redis.sadd(self._workspaces_key(), workspace)

        token_to_id: dict[str, str] = {}
        entity_hash_writes: list[tuple[str, str, str]] = []
        existing_entities = await self._batch_hget(
            key=entity_key,
            fields=list(entity_tokens.keys()),
        )
        for scoped_token, display_name in entity_tokens.items():
            entity_id = existing_entities.get(scoped_token)
            if entity_id is None:
                entity_id = _canonical_id(doc_id, display_name)
                entity_hash_writes.append((entity_key, scoped_token, entity_id))
                created_entities += 1
            else:
                reused_entities += 1
            token_to_id[scoped_token] = str(entity_id)
        if entity_hash_writes:
            await self._execute_writes(hash_writes=entity_hash_writes, set_writes=[])

        edge_accumulator: dict[str, dict[str, Any]] = {}
        for row in normalized:
            src_id = token_to_id[row["src_scope"]]
            tgt_id = token_to_id[row["tgt_scope"]]
            eid = _edge_id(doc_id, src_id, row["rel_type"], tgt_id)
            item = edge_accumulator.get(eid)
            if item is None:
                item = {
                    "edge_id": eid,
                    "doc_id": doc_id,
                    "source_entity": row["src_name"],
                    "target_entity": row["tgt_name"],
                    "source_canonical_id": src_id,
                    "target_canonical_id": tgt_id,
                    "relation_type": row["rel_type"],
                    "relation_description": row["rel_desc"],
                    "source_file_path": row["source_file_path"],
                    "occurrence": 0,
                    "source_refs": set(),
                }
                edge_accumulator[eid] = item
            item["occurrence"] = int(item["occurrence"]) + 1
            if row["source_ref"]:
                item["source_refs"].add(row["source_ref"])
            if row["rel_desc"]:
                item["relation_description"] = row["rel_desc"]
            if row["source_file_path"] and not item["source_file_path"]:
                item["source_file_path"] = row["source_file_path"]

        edge_hash_writes: list[tuple[str, str, str]] = []
        doc_set_writes: list[tuple[str, str]] = []
        now_ms = int(time.time() * 1000)
        existing_edges = await self._batch_hget(
            key=edge_key,
            fields=list(edge_accumulator.keys()),
        )
        for eid, item in edge_accumulator.items():
            edge_raw = existing_edges.get(eid)
            incoming_occurrence = int(item["occurrence"])
            incoming_refs = sorted(item["source_refs"])
            if edge_raw is None:
                edge_payload = {
                    "edge_id": eid,
                    "doc_id": item["doc_id"],
                    "source_entity": item["source_entity"],
                    "target_entity": item["target_entity"],
                    "source_canonical_id": item["source_canonical_id"],
                    "target_canonical_id": item["target_canonical_id"],
                    "relation_type": item["relation_type"],
                    "relation_description": str(item["relation_description"] or ""),
                    "source_file_path": str(item["source_file_path"] or ""),
                    "occurrence": incoming_occurrence,
                    "source_refs": incoming_refs,
                    "status": UNVERIFIED,
                    "compile_phase": compile_phase,
                    "batch_id": "",
                    "updated_at": now_ms,
                }
                created_edges += 1
                absorbed_edges += max(0, incoming_occurrence - 1)
            else:
                edge_payload = json.loads(edge_raw)
                if str(edge_payload.get("status") or "") == MIGRATING:
                    pending = edge_payload.get("pending")
                    if not isinstance(pending, dict):
                        pending = {
                            "occurrence": 0,
                            "source_refs": [],
                            "relation_description": "",
                            "source_file_path": "",
                        }
                    pending_refs = list(pending.get("source_refs") or [])
                    for ref in incoming_refs:
                        if ref not in pending_refs:
                            pending_refs.append(ref)
                    pending["source_refs"] = pending_refs
                    pending["occurrence"] = (
                        int(pending.get("occurrence") or 0) + incoming_occurrence
                    )
                    pending["relation_description"] = str(
                        pending.get("relation_description") or item["relation_description"] or ""
                    )
                    if not str(pending.get("source_file_path") or "").strip():
                        pending["source_file_path"] = str(item["source_file_path"] or "")
                    edge_payload["pending"] = pending
                    edge_payload["updated_at"] = now_ms
                    edge_hash_writes.append(
                        (edge_key, eid, json.dumps(edge_payload, ensure_ascii=False))
                    )
                    doc_set_writes.append((self._doc_edges_key(workspace, doc_id), eid))
                    continue
                refs = list(edge_payload.get("source_refs") or [])
                for ref in incoming_refs:
                    if ref not in refs:
                        refs.append(ref)
                edge_payload["source_refs"] = refs
                edge_payload["occurrence"] = (
                    int(edge_payload.get("occurrence") or 0) + incoming_occurrence
                )
                edge_payload["relation_description"] = str(
                    edge_payload.get("relation_description") or item["relation_description"] or ""
                )
                if not str(edge_payload.get("source_file_path") or "").strip():
                    edge_payload["source_file_path"] = str(item["source_file_path"] or "")
                edge_payload["updated_at"] = now_ms
                absorbed_edges += incoming_occurrence
            edge_hash_writes.append((edge_key, eid, json.dumps(edge_payload, ensure_ascii=False)))
            doc_set_writes.append((self._doc_edges_key(workspace, doc_id), eid))

        await self._execute_writes(hash_writes=edge_hash_writes, set_writes=doc_set_writes)

        total = created_edges + absorbed_edges
        return {
            "workspace": workspace,
            "doc_id": doc_id,
            "total_relations": total,
            "created_edges": created_edges,
            "absorbed_edges": absorbed_edges,
            "created_entities": created_entities,
            "reused_entities": reused_entities,
            "duplicate_absorb_rate": round(absorbed_edges / total, 4) if total else 0.0,
        }

    async def list_workspaces(self) -> list[str]:
        return sorted(
            {workspace for workspace in await self._smembers(self._workspaces_key()) if workspace}
        )

    async def unverified_edge_count(self, *, workspace: str) -> int:
        edge_map = await self.redis.hgetall(self._edge_map_key(workspace))
        return sum(
            1 for raw in edge_map.values() if str(json.loads(raw).get("status") or "") == UNVERIFIED
        )

    async def delete_doc_relations(
        self,
        *,
        workspace: str,
        doc_id: str,
    ) -> dict[str, Any]:
        normalized_doc_id = str(doc_id or "").strip()
        if not normalized_doc_id:
            return {
                "workspace": workspace,
                "doc_id": "",
                "deleted_edges": 0,
                "updated_edges": 0,
                "removed_refs": 0,
            }

        edge_key = self._edge_map_key(workspace)
        doc_key = self._doc_edges_key(workspace, normalized_doc_id)
        doc_ref_prefix = f"{normalized_doc_id}:"

        doc_edge_ids = [item for item in await self._smembers(doc_key) if item]
        edge_payloads: dict[str, Any] = {}
        if doc_edge_ids:
            edge_payloads = await self._batch_hget(key=edge_key, fields=doc_edge_ids)

        # 兜底扫描，避免历史数据缺少 doc_edges 索引时漏删。
        scanned_all = False
        if not edge_payloads:
            edge_payloads = await self.redis.hgetall(edge_key)
            scanned_all = True

        now_ms = int(time.time() * 1000)
        deleted_edges = 0
        updated_edges = 0
        removed_refs = 0

        affected_edge_ids: list[str] = []
        for eid, raw in edge_payloads.items():
            edge_id = str(eid or "").strip()
            if not edge_id or raw is None:
                continue
            payload = json.loads(raw)
            refs = [
                str(ref).strip() for ref in payload.get("source_refs") or [] if str(ref).strip()
            ]
            if not refs:
                continue
            keep_refs = [ref for ref in refs if not ref.startswith(doc_ref_prefix)]
            if len(keep_refs) == len(refs):
                continue
            removed_refs += len(refs) - len(keep_refs)
            affected_edge_ids.append(edge_id)
            if not keep_refs:
                await self.redis.hdel(edge_key, edge_id)
                deleted_edges += 1
                continue
            payload["source_refs"] = keep_refs
            payload["occurrence"] = max(1, min(int(payload.get("occurrence") or 0), len(keep_refs)))
            payload["updated_at"] = now_ms
            payload["status"] = UNVERIFIED
            payload["batch_id"] = ""
            await self.redis.hset(edge_key, edge_id, json.dumps(payload, ensure_ascii=False))
            updated_edges += 1

        remover = getattr(self.redis, "srem", None)
        if callable(remover):
            for edge_id in affected_edge_ids:
                await remover(doc_key, edge_id)
        deleter = getattr(self.redis, "delete", None)
        if callable(deleter):
            await deleter(doc_key)

        return {
            "workspace": workspace,
            "doc_id": normalized_doc_id,
            "deleted_edges": deleted_edges,
            "updated_edges": updated_edges,
            "removed_refs": removed_refs,
            "candidate_edge_ids": len(doc_edge_ids),
            "scanned_all_edges": scanned_all,
        }

    async def query_delta_chunks(
        self,
        *,
        workspace: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        graph_hits = await self.query_delta_graph(
            workspace=workspace,
            query=query,
            top_k_relations=top_k,
            top_k_entities=max(1, top_k),
        )
        return list(graph_hits.get("chunks") or [])

    @staticmethod
    def _source_chunk_ids_from_refs(source_refs: list[str]) -> list[str]:
        chunk_ids: list[str] = []
        seen: set[str] = set()
        for ref in source_refs:
            value = str(ref or "").strip()
            if not value:
                continue
            chunk_id = value.split(":", 1)[1].strip() if ":" in value else value
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            chunk_ids.append(chunk_id)
        return chunk_ids

    @staticmethod
    def _graph_source_id(chunk_ids: list[str]) -> str:
        return "<SEP>".join(
            [str(chunk_id).strip() for chunk_id in chunk_ids if str(chunk_id).strip()]
        )

    async def query_delta_graph(
        self,
        *,
        workspace: str,
        query: str,
        top_k_relations: int,
        top_k_entities: int,
    ) -> dict[str, Any]:
        relation_k = max(0, int(top_k_relations))
        entity_k = max(0, int(top_k_entities))
        if relation_k <= 0:
            return {"chunks": [], "entities": [], "relationships": [], "stats": {}}
        terms = _split_query_terms(query)
        if not terms:
            return {"chunks": [], "entities": [], "relationships": [], "stats": {}}

        edge_key = self._edge_map_key(workspace)
        edge_map = await self.redis.hgetall(edge_key)
        rows: list[tuple[float, dict[str, Any]]] = []
        for raw in edge_map.values():
            payload = json.loads(raw)
            status = str(payload.get("status") or "")
            if status == MIGRATING:
                continue
            text = " ".join(
                [
                    str(payload.get("source_entity") or ""),
                    str(payload.get("relation_type") or ""),
                    str(payload.get("target_entity") or ""),
                    str(payload.get("relation_description") or ""),
                ]
            ).lower()
            score = 0.0
            for term in terms:
                if term in text:
                    score += 1.0
            if score <= 0:
                continue
            score += min(3.0, float(payload.get("occurrence") or 0) * 0.1)
            rows.append((score, payload))

        rows.sort(key=lambda item: item[0], reverse=True)
        selected = rows[:relation_k]
        chunk_rows: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        entity_accumulator: dict[str, dict[str, Any]] = {}

        for score, payload in selected:
            refs = [str(ref) for ref in payload.get("source_refs") or [] if str(ref).strip()]
            doc_id = refs[0].split(":", 1)[0] if refs else "unknown"
            source_chunk_ids = self._source_chunk_ids_from_refs(refs)
            source_id = self._graph_source_id(source_chunk_ids)
            weight = round(score, 4)
            source_entity = str(payload.get("source_entity") or "")
            target_entity = str(payload.get("target_entity") or "")
            source_file_path = str(payload.get("source_file_path") or "")

            relationships.append(
                {
                    "src_id": source_entity,
                    "tgt_id": target_entity,
                    "description": str(payload.get("relation_description") or ""),
                    "weight": weight,
                    "source_id": source_id,
                    "file_path": source_file_path or f"hot_buffer://{workspace}/{doc_id}",
                    "keywords": [str(payload.get("relation_type") or "")]
                    if payload.get("relation_type")
                    else [],
                    "situation": str(payload.get("compile_phase") or ""),
                }
            )

            for entity_name, canonical_id in [
                (source_entity, str(payload.get("source_canonical_id") or "")),
                (target_entity, str(payload.get("target_canonical_id") or "")),
            ]:
                if not entity_name:
                    continue
                key = canonical_id or _normalize_text(entity_name)
                current = entity_accumulator.get(key)
                if current is None:
                    entity_accumulator[key] = {
                        "entity_name": entity_name,
                        "entity_type": "Concept",
                        "description": "",
                        "source_id": source_id,
                        "file_path": source_file_path or f"hot_buffer://{workspace}/{doc_id}",
                        "weight": weight,
                    }
                    continue
                current["weight"] = max(float(current.get("weight") or 0.0), weight)
                current["source_id"] = self._graph_source_id(
                    [*current.get("source_id", "").split("<SEP>"), *source_chunk_ids]
                )

            chunk_rows.append(
                {
                    "chunk_id": f"vkr-delta:{payload['edge_id']}",
                    "content": (
                        f"{payload.get('source_entity')} -> {payload.get('relation_type')} -> "
                        f"{payload.get('target_entity')}"
                    ),
                    "context": str(payload.get("relation_description") or ""),
                    "tokens": 0,
                    "full_doc_id": doc_id,
                    "order": 0,
                    "file_path": source_file_path or f"hot_buffer://{workspace}/{doc_id}",
                    "metadata": {
                        "chunk_type": "delta_relation",
                        "vkr_source": "delta_hot_buffer",
                        "score": weight,
                        "occurrence": int(payload.get("occurrence") or 0),
                        "status": str(payload.get("status") or UNVERIFIED),
                        "compile_phase": str(payload.get("compile_phase") or ""),
                        "canonical_ids": {
                            "source": payload.get("source_canonical_id"),
                            "target": payload.get("target_canonical_id"),
                        },
                        "source_refs": refs,
                        "source_chunk_ids": source_chunk_ids,
                        "title": "Fast Ingest Delta",
                    },
                }
            )

        entities = sorted(
            entity_accumulator.values(),
            key=lambda item: float(item.get("weight") or 0.0),
            reverse=True,
        )
        if entity_k > 0:
            entities = entities[:entity_k]
        return {
            "chunks": chunk_rows,
            "entities": entities,
            "relationships": relationships,
            "stats": {
                "matched_relations": len(selected),
                "matched_entities": len(entities),
                "candidate_relations": len(rows),
            },
        }

    async def prepare_post_compile_batch(
        self,
        *,
        workspace: str,
        batch_size: int,
    ) -> dict[str, Any]:
        async with self._workspace_lock(workspace):
            return await self._prepare_post_compile_batch(
                workspace=workspace,
                batch_size=batch_size,
            )

    async def _prepare_post_compile_batch(
        self,
        *,
        workspace: str,
        batch_size: int,
    ) -> dict[str, Any]:
        edge_key = self._edge_map_key(workspace)
        edge_map = await self.redis.hgetall(edge_key)
        if batch_size <= 0:
            return {"workspace": workspace, "batch_id": "", "edges": []}

        candidates: list[tuple[str, dict[str, Any]]] = []
        for eid, raw in edge_map.items():
            payload = json.loads(raw)
            if str(payload.get("status") or "") != UNVERIFIED:
                continue
            candidates.append((eid, payload))

        candidates.sort(key=lambda item: int(item[1].get("updated_at") or 0), reverse=True)
        selected = candidates[:batch_size]
        if not selected:
            return {"workspace": workspace, "batch_id": "", "edges": []}

        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        now_ms = int(time.time() * 1000)
        out_edges: list[dict[str, Any]] = []
        for eid, payload in selected:
            payload["status"] = MIGRATING
            payload["batch_id"] = batch_id
            payload["merge_token"] = str(payload.get("merge_token") or f"merge-{uuid.uuid4().hex}")
            payload["updated_at"] = now_ms
            await self.redis.hset(edge_key, eid, json.dumps(payload, ensure_ascii=False))
            out_edges.append(payload)
        return {"workspace": workspace, "batch_id": batch_id, "edges": out_edges}

    async def finish_post_compile_batch(
        self,
        *,
        workspace: str,
        batch_id: str,
        success: bool,
    ) -> dict[str, Any]:
        async with self._workspace_lock(workspace):
            return await self._finish_post_compile_batch(
                workspace=workspace,
                batch_id=batch_id,
                success=success,
            )

    async def _finish_post_compile_batch(
        self,
        *,
        workspace: str,
        batch_id: str,
        success: bool,
    ) -> dict[str, Any]:
        edge_key = self._edge_map_key(workspace)
        edge_map = await self.redis.hgetall(edge_key)
        updated = 0
        removed = 0
        for eid, raw in edge_map.items():
            payload = json.loads(raw)
            if str(payload.get("batch_id") or "") != batch_id:
                continue
            if success:
                pending = payload.get("pending")
                if isinstance(pending, dict) and int(pending.get("occurrence") or 0) > 0:
                    payload["occurrence"] = int(pending["occurrence"])
                    payload["source_refs"] = list(pending.get("source_refs") or [])
                    payload["relation_description"] = str(
                        pending.get("relation_description")
                        or payload.get("relation_description")
                        or ""
                    )
                    payload["source_file_path"] = str(
                        pending.get("source_file_path") or payload.get("source_file_path") or ""
                    )
                    payload["status"] = UNVERIFIED
                    payload["batch_id"] = ""
                    payload.pop("merge_token", None)
                    payload.pop("pending", None)
                    payload["updated_at"] = int(time.time() * 1000)
                    await self.redis.hset(edge_key, eid, json.dumps(payload, ensure_ascii=False))
                    updated += 1
                else:
                    await self.redis.hdel(edge_key, eid)
                    removed += 1
            else:
                pending = payload.pop("pending", None)
                if isinstance(pending, dict):
                    refs = list(payload.get("source_refs") or [])
                    for ref in pending.get("source_refs") or []:
                        if ref not in refs:
                            refs.append(ref)
                    payload["source_refs"] = refs
                    payload["occurrence"] = int(payload.get("occurrence") or 0) + int(
                        pending.get("occurrence") or 0
                    )
                    payload["relation_description"] = str(
                        payload.get("relation_description")
                        or pending.get("relation_description")
                        or ""
                    )
                    if not str(payload.get("source_file_path") or "").strip():
                        payload["source_file_path"] = str(pending.get("source_file_path") or "")
                payload["status"] = UNVERIFIED
                payload["batch_id"] = ""
                payload["updated_at"] = int(time.time() * 1000)
                await self.redis.hset(edge_key, eid, json.dumps(payload, ensure_ascii=False))
                updated += 1
        return {
            "workspace": workspace,
            "batch_id": batch_id,
            "success": success,
            "removed_edges": removed,
            "restored_edges": updated,
        }

    async def lint_promote(
        self,
        *,
        workspace: str,
        max_edges: int,
    ) -> dict[str, Any]:
        if max_edges <= 0:
            return {"workspace": workspace, "selected": 0, "verified": 0, "conflict_hold": 0}
        edge_key = self._edge_map_key(workspace)
        edge_map = await self.redis.hgetall(edge_key)

        pending: list[tuple[str, dict[str, Any]]] = []
        for eid, raw in edge_map.items():
            payload = json.loads(raw)
            if str(payload.get("status") or "") == PENDING_LINT:
                pending.append((eid, payload))
        pending.sort(key=lambda item: int(item[1].get("updated_at") or 0), reverse=True)
        selected = pending[:max_edges]

        verified = 0
        conflict = 0
        for eid, payload in selected:
            payload["status"] = LINTING
            await self.redis.hset(edge_key, eid, json.dumps(payload, ensure_ascii=False))

            relation_desc = _normalize_text(str(payload.get("relation_description") or ""))
            if relation_desc == "":
                payload["status"] = CONFLICT_HOLD
                conflict += 1
            else:
                payload["status"] = VERIFIED
                verified += 1
            payload["updated_at"] = int(time.time() * 1000)
            await self.redis.hset(edge_key, eid, json.dumps(payload, ensure_ascii=False))

        return {
            "workspace": workspace,
            "selected": len(selected),
            "verified": verified,
            "conflict_hold": conflict,
        }

    async def mark_batch_pending_lint(
        self,
        *,
        workspace: str,
        batch_id: str,
    ) -> dict[str, Any]:
        edge_key = self._edge_map_key(workspace)
        edge_map = await self.redis.hgetall(edge_key)
        updated = 0
        for eid, raw in edge_map.items():
            payload = json.loads(raw)
            if str(payload.get("batch_id") or "") != batch_id:
                continue
            payload["status"] = PENDING_LINT
            payload["updated_at"] = int(time.time() * 1000)
            await self.redis.hset(edge_key, eid, json.dumps(payload, ensure_ascii=False))
            updated += 1
        return {"workspace": workspace, "batch_id": batch_id, "updated": updated}
