"""Knowledge library import orchestration (idempotent merge)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viknow.core.knowledge.migration import idgen, pathmap
from viknow.core.knowledge.migration.models import (
    EmbeddingFingerprint,
    MigrationManifest,
    ProgressCallback,
    report_progress,
)
from viknow.core.knowledge.migration.package import extract_package, iter_jsonl
from viknow.core.knowledge.migration.storage import (
    MigrationNeo4j,
    MigrationPostgres,
    MigrationStorageConfig,
    build_video_segment_table_name,
    parse_vector_literal,
    vector_literal,
)
from viknow.core.knowledge.uploads.domain import KnowledgeUploadRecord
from viknow.core.knowledge.uploads.store import KnowledgeUploadStore


class EmbeddingMismatchError(ValueError):
    """Raised when package embedding fingerprint differs from the target env."""


@dataclass
class ImporterDeps:
    files_root: Path
    storage_config: MigrationStorageConfig
    upload_store: KnowledgeUploadStore


def _ensure_fingerprint_compatible(
    manifest: MigrationManifest, config: MigrationStorageConfig
) -> None:
    video_table = build_video_segment_table_name(
        base_table=config.video_segment_table_base,
        model_name=config.video_embedding_model,
        vector_dim=config.video_embedding_dim,
    )
    target = EmbeddingFingerprint(
        embedding_dim=config.embedding_dim,
        video_embedding_model=config.video_embedding_model,
        video_embedding_dim=config.video_embedding_dim,
        video_segment_table=video_table,
    )
    mismatches: list[str] = []
    if manifest.embedding.embedding_dim != target.embedding_dim:
        mismatches.append(
            f"embedding_dim {manifest.embedding.embedding_dim} != {target.embedding_dim}"
        )
    if manifest.embedding.video_embedding_model != target.video_embedding_model:
        mismatches.append(
            f"video_embedding_model {manifest.embedding.video_embedding_model!r} != "
            f"{target.video_embedding_model!r}"
        )
    if manifest.embedding.video_embedding_dim != target.video_embedding_dim:
        mismatches.append(
            f"video_embedding_dim {manifest.embedding.video_embedding_dim} != "
            f"{target.video_embedding_dim}"
        )
    if manifest.embedding.video_segment_table != target.video_segment_table:
        mismatches.append(
            f"video_segment_table {manifest.embedding.video_segment_table!r} != "
            f"{target.video_segment_table!r}"
        )
    if mismatches:
        raise EmbeddingMismatchError("embedding fingerprint mismatch: " + "; ".join(mismatches))


def _rewrite_to_absolute(value: Any, *, files_root: Path, library_id: str) -> Any:
    """Rewrite path-like JSON values into target absolute paths."""
    return pathmap.rewrite_json_paths(
        value,
        rewrite=lambda logical: pathmap.to_absolute_path(
            logical, files_root=str(files_root), library_id=library_id
        ),
    )


def _restore_upload_records(
    rows: list[dict[str, Any]],
    *,
    deps: ImporterDeps,
    target_library_id: str,
) -> int:
    """Upsert knowledge upload records for the target library (idempotent)."""
    restored = 0
    for raw in rows:
        record = KnowledgeUploadRecord.model_validate(raw)
        object_key = record.object_key
        if object_key and pathmap.is_logical_path(object_key):
            object_key = (
                Path(
                    pathmap.to_absolute_path(
                        object_key, files_root=str(deps.files_root), library_id=target_library_id
                    )
                )
                .relative_to(deps.files_root)
                .as_posix()
            )
        local_path = record.local_path
        if local_path and pathmap.is_logical_path(local_path):
            local_path = pathmap.to_absolute_path(
                local_path, files_root=str(deps.files_root), library_id=target_library_id
            )
        restored_record = record.model_copy(
            update={
                "library_id": target_library_id,
                "object_key": object_key or record.object_key,
                "local_path": local_path or record.local_path,
            }
        )
        existing = deps.upload_store.get(restored_record.id, include_deleted=True)
        if existing is None:
            deps.upload_store.add(restored_record)
        else:
            deps.upload_store.update(restored_record)
        restored += 1
    return restored


async def import_package(
    *,
    deps: ImporterDeps,
    package_path: Path,
    target_library_id: str,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Import a knowledge package into the target library idempotently."""
    lock = await _workspace_lock(target_library_id)
    async with lock:
        return await _import_package_locked(
            deps=deps,
            package_path=package_path,
            target_library_id=target_library_id,
            progress=progress,
        )


async def _import_package_locked(
    *,
    deps: ImporterDeps,
    package_path: Path,
    target_library_id: str,
    progress: ProgressCallback | None,
) -> dict[str, Any]:
    """Import body guarded by the per-workspace in-process lock."""
    await report_progress(progress, "verify", 0.05, "校验包完整性")
    with tempfile.TemporaryDirectory(prefix="viknow-migrate-") as tmp_text:
        tmp_dir = Path(tmp_text)
        manifest = await asyncio.to_thread(extract_package, package_path, tmp_dir)

        await report_progress(progress, "fingerprint", 0.15, "校验 embedding 指纹")
        _ensure_fingerprint_compatible(manifest, deps.storage_config)

        await report_progress(progress, "files", 0.3, "落盘原始文件")
        restore_count = await asyncio.to_thread(
            _restore_files_sync,
            manifest,
            tmp_dir=tmp_dir,
            deps=deps,
            target_library_id=target_library_id,
        )

        pg = MigrationPostgres(deps.storage_config)
        neo4j = MigrationNeo4j(deps.storage_config)
        try:
            await report_progress(progress, "pg", 0.55, "合并向量/状态数据")
            await _import_pg(
                pg,
                package_path=package_path,
                deps=deps,
                target_library_id=target_library_id,
            )
            await report_progress(progress, "graph", 0.8, "合并知识图谱")
            await _import_graph(
                neo4j,
                package_path=package_path,
                deps=deps,
                target_library_id=target_library_id,
            )
        finally:
            await pg.close()
            await neo4j.close()

        await report_progress(progress, "uploads", 0.9, "恢复上传记录")
        upload_rows = list(iter_jsonl(package_path, "pg/knowledge_uploads.jsonl"))
        restored_uploads = await asyncio.to_thread(
            _restore_upload_records,
            upload_rows,
            deps=deps,
            target_library_id=target_library_id,
        )

        await report_progress(progress, "done", 1.0, "导入完成")
        return {
            "target_library_id": target_library_id,
            "restored_files": restore_count,
            "restored_uploads": restored_uploads,
            "table_counts": manifest.table_counts,
        }


_workspace_locks: dict[str, asyncio.Lock] = {}
_workspace_locks_guard = asyncio.Lock()


async def _workspace_lock(workspace: str) -> asyncio.Lock:
    async with _workspace_locks_guard:
        lock = _workspace_locks.get(workspace)
        if lock is None:
            lock = asyncio.Lock()
            _workspace_locks[workspace] = lock
        return lock


def _restore_files_sync(
    manifest: MigrationManifest,
    *,
    tmp_dir: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> int:
    restored = 0
    for entry in manifest.files:
        source = tmp_dir / "files" / entry.logical_path
        if not source.is_file():
            continue
        target = Path(
            pathmap.to_absolute_path(
                entry.logical_path, files_root=str(deps.files_root), library_id=target_library_id
            )
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp sibling first, then rename, so a partial file never
        # remains at the final path if the process is interrupted mid-copy.
        staging = target.parent / f".{target.name}.tmp-{tempfile.mktemp(prefix='')[6:]}"
        shutil.copy2(source, staging)
        os.replace(staging, target)
        restored += 1
    return restored


async def _restore_files(
    manifest: MigrationManifest,
    *,
    tmp_dir: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> int:
    return await asyncio.to_thread(
        _restore_files_sync,
        manifest,
        tmp_dir=tmp_dir,
        deps=deps,
        target_library_id=target_library_id,
    )


async def _import_pg(
    pg: MigrationPostgres,
    *,
    package_path: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> dict[str, str]:
    config = pg.config
    chunk_map: dict[str, str] = {}

    kg_tables = {
        "kg_chunk_vectors": config.kg_chunk_table,
        "kg_entity_vectors": config.kg_entity_table,
        "kg_relation_vectors": config.kg_relation_table,
    }
    for member, table in kg_tables.items():
        rows = list(iter_jsonl(package_path, f"pg/{member}.jsonl"))
        if member == "kg_chunk_vectors":
            chunk_map = await _merge_chunk_rows(
                pg, rows, table=table, deps=deps, target_library_id=target_library_id
            )
        elif member == "kg_entity_vectors":
            await _merge_entity_rows(
                pg, rows, table=table, deps=deps, target_library_id=target_library_id
            )
        else:
            await _merge_relation_rows(
                pg, rows, table=table, deps=deps, target_library_id=target_library_id
            )

    await _merge_asset_status_rows(pg, package_path, deps, target_library_id)
    await _merge_bm25_rows(pg, package_path, deps, target_library_id, chunk_map)
    await _merge_video_segment_rows(pg, package_path, deps, target_library_id)
    await _merge_knowledgebase_info_rows(pg, package_path, deps, target_library_id)
    return chunk_map


def _target_abs(logical_path: str | None, *, deps: ImporterDeps, target_library_id: str) -> str:
    if not logical_path:
        return ""
    return pathmap.to_absolute_path(
        logical_path, files_root=str(deps.files_root), library_id=target_library_id
    )


async def _merge_chunk_rows(
    pg: MigrationPostgres,
    rows: list[dict[str, Any]],
    *,
    table: str,
    deps: ImporterDeps,
    target_library_id: str,
) -> dict[str, str]:
    chunk_map: dict[str, str] = {}
    payloads: list[list[Any]] = []
    schema = pg.config.kg_schema
    for row in rows:
        old_id = str(row.get("item_id") or "")
        logical = str(row.get("file_path") or "")
        target_abs = _target_abs(logical, deps=deps, target_library_id=target_library_id)
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        order = int(metadata.get("order") or 0)
        text = str(metadata.get("text") or row.get("content") or "")
        new_id = idgen.recompute_chunk_id(file_path=target_abs, order=order, text=text)
        chunk_map[old_id] = new_id
        metadata["chunk_id"] = new_id
        metadata = _rewrite_to_absolute(
            metadata, files_root=deps.files_root, library_id=target_library_id
        )
        embedding = parse_vector_literal(row.get("embedding"))
        payloads.append(
            [
                target_library_id,
                new_id,
                target_abs,
                str(row.get("content") or ""),
                vector_literal(embedding),
                json.dumps(metadata, ensure_ascii=False),
            ]
        )
    if payloads:
        sql = f"""
            INSERT INTO {schema}.{table}
                (workspace, item_id, file_path, content, embedding, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
            ON CONFLICT (workspace, item_id)
            DO UPDATE SET
                file_path = EXCLUDED.file_path,
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
        """
        await pg.execute_many(sql, payloads)
    return chunk_map


async def _merge_entity_rows(
    pg: MigrationPostgres,
    rows: list[dict[str, Any]],
    *,
    table: str,
    deps: ImporterDeps,
    target_library_id: str,
) -> None:
    payloads: list[list[Any]] = []
    schema = pg.config.kg_schema
    for row in rows:
        target_abs = _target_abs(
            str(row.get("file_path") or ""), deps=deps, target_library_id=target_library_id
        )
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        new_id = idgen.recompute_entity_node_id(
            file_path=target_abs,
            entity_type=str(metadata.get("entity_type") or ""),
            entity_name=str(metadata.get("entity_name") or ""),
        )
        metadata["node_id"] = new_id
        metadata = _rewrite_to_absolute(
            metadata, files_root=deps.files_root, library_id=target_library_id
        )
        embedding = parse_vector_literal(row.get("embedding"))
        payloads.append(
            [
                target_library_id,
                new_id,
                target_abs,
                str(row.get("content") or ""),
                vector_literal(embedding),
                json.dumps(metadata, ensure_ascii=False),
            ]
        )
    if payloads:
        sql = f"""
            INSERT INTO {schema}.{table}
                (workspace, item_id, file_path, content, embedding, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
            ON CONFLICT (workspace, item_id)
            DO UPDATE SET
                file_path = EXCLUDED.file_path,
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
        """
        await pg.execute_many(sql, payloads)


async def _merge_relation_rows(
    pg: MigrationPostgres,
    rows: list[dict[str, Any]],
    *,
    table: str,
    deps: ImporterDeps,
    target_library_id: str,
) -> None:
    payloads: list[list[Any]] = []
    schema = pg.config.kg_schema
    for row in rows:
        target_abs = _target_abs(
            str(row.get("file_path") or ""), deps=deps, target_library_id=target_library_id
        )
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        new_id = idgen.recompute_relation_id(
            file_path=target_abs,
            source_entity=str(metadata.get("source_entity") or ""),
            target_entity=str(metadata.get("target_entity") or ""),
        )
        metadata["relation_id"] = new_id
        metadata = _rewrite_to_absolute(
            metadata, files_root=deps.files_root, library_id=target_library_id
        )
        embedding = parse_vector_literal(row.get("embedding"))
        payloads.append(
            [
                target_library_id,
                new_id,
                target_abs,
                str(row.get("content") or ""),
                vector_literal(embedding),
                json.dumps(metadata, ensure_ascii=False),
            ]
        )
    if payloads:
        sql = f"""
            INSERT INTO {schema}.{table}
                (workspace, item_id, file_path, content, embedding, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
            ON CONFLICT (workspace, item_id)
            DO UPDATE SET
                file_path = EXCLUDED.file_path,
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
        """
        await pg.execute_many(sql, payloads)


async def _merge_asset_status_rows(
    pg: MigrationPostgres,
    package_path: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> None:
    rows = list(iter_jsonl(package_path, "pg/asset_status.jsonl"))
    payloads: list[list[Any]] = []
    for row in rows:
        logical = str(row.get("file_path") or "")
        target_abs = _target_abs(logical, deps=deps, target_library_id=target_library_id)
        metadata = _rewrite_to_absolute(
            row.get("metadata") or {}, files_root=deps.files_root, library_id=target_library_id
        )
        file_data = _rewrite_to_absolute(
            row.get("file_data") or {}, files_root=deps.files_root, library_id=target_library_id
        )
        payloads.append(
            [
                target_library_id,
                str(row.get("id") or ""),
                str(row.get("asset_id") or ""),
                str(row.get("status") or ""),
                str(row.get("created_at") or ""),
                str(row.get("updated_at") or ""),
                float(row.get("progress") or 0.0),
                row.get("error_message"),
                row.get("parse_mode"),
                target_abs,
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(file_data, ensure_ascii=False),
            ]
        )
    if payloads:
        sql = """
            INSERT INTO public.hyperrag_asset_status
                (workspace, id, asset_id, status, created_at, updated_at, progress,
                 error_message, parse_mode, file_path, metadata, file_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (workspace, id)
            DO UPDATE SET
                asset_id = EXCLUDED.asset_id,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at,
                progress = EXCLUDED.progress,
                error_message = EXCLUDED.error_message,
                parse_mode = EXCLUDED.parse_mode,
                file_path = EXCLUDED.file_path,
                metadata = EXCLUDED.metadata,
                file_data = EXCLUDED.file_data
        """
        await pg.execute_many(sql, payloads)


async def _merge_bm25_rows(
    pg: MigrationPostgres,
    package_path: Path,
    deps: ImporterDeps,
    target_library_id: str,
    chunk_map: dict[str, str],
) -> None:
    rows = list(iter_jsonl(package_path, "pg/bm25_chunks.jsonl"))
    payloads: list[list[Any]] = []
    for row in rows:
        old_id = str(row.get("id") or "")
        new_id = chunk_map.get(old_id, old_id)
        payloads.append(
            [
                target_library_id,
                new_id,
                row.get("full_doc_id"),
                row.get("chunk_order_index"),
                str(row.get("content") or ""),
                str(row.get("content_tokenized") or ""),
                row.get("tokens"),
            ]
        )
    if payloads:
        sql = """
            INSERT INTO public.hyperrag_bm25_chunks
                (workspace, id, full_doc_id, chunk_order_index, content, content_tokenized, tokens)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace, id)
            DO UPDATE SET
                full_doc_id = EXCLUDED.full_doc_id,
                chunk_order_index = EXCLUDED.chunk_order_index,
                content = EXCLUDED.content,
                content_tokenized = EXCLUDED.content_tokenized,
                tokens = EXCLUDED.tokens
        """
        await pg.execute_many(sql, payloads)


async def _merge_video_segment_rows(
    pg: MigrationPostgres,
    package_path: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> None:
    config = pg.config
    table = build_video_segment_table_name(
        base_table=config.video_segment_table_base,
        model_name=config.video_embedding_model,
        vector_dim=config.video_embedding_dim,
    )
    rows = list(iter_jsonl(package_path, "pg/video_segments.jsonl"))
    payloads: list[list[Any]] = []
    for row in rows:
        metadata = _rewrite_to_absolute(
            row.get("metadata") or {}, files_root=deps.files_root, library_id=target_library_id
        )
        embedding = parse_vector_literal(row.get("content_vector"))
        payloads.append(
            [
                target_library_id,
                str(row.get("id") or ""),
                str(row.get("video_name") or ""),
                int(row.get("segment_index") or 0),
                vector_literal(embedding),
                json.dumps(metadata, ensure_ascii=False),
            ]
        )
    if payloads:
        sql = f"""
            INSERT INTO public.{table}
                (workspace, id, video_name, segment_index, content_vector, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
            ON CONFLICT (workspace, id)
            DO UPDATE SET
                video_name = EXCLUDED.video_name,
                segment_index = EXCLUDED.segment_index,
                content_vector = EXCLUDED.content_vector,
                metadata = EXCLUDED.metadata
        """
        await pg.execute_many(sql, payloads)


async def _merge_knowledgebase_info_rows(
    pg: MigrationPostgres,
    package_path: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> None:
    rows = list(iter_jsonl(package_path, "pg/knowledgebase_info.jsonl"))
    if not rows:
        return
    payloads: list[list[Any]] = []
    for row in rows:
        payloads.append(
            [
                target_library_id,
                target_library_id,
                row.get("created_at") or None,
                row.get("updated_at") or None,
                json.dumps(row.get("asset_ids") or [], ensure_ascii=False),
                row.get("summarization") or None,
                json.dumps(row.get("metadata") or {}, ensure_ascii=False),
            ]
        )
    sql = """
        INSERT INTO public.hyperrag_knowledgebase_info
            (id, name, created_at, updated_at, asset_ids, summarization, metadata)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
        ON CONFLICT (id)
        DO UPDATE SET
            name = EXCLUDED.name,
            asset_ids = EXCLUDED.asset_ids,
            summarization = EXCLUDED.summarization,
            metadata = EXCLUDED.metadata,
            updated_at = EXCLUDED.updated_at
    """
    await pg.execute_many(sql, payloads)


async def _import_graph(
    neo4j: MigrationNeo4j,
    *,
    package_path: Path,
    deps: ImporterDeps,
    target_library_id: str,
) -> None:
    entities = list(iter_jsonl(package_path, "graph/entities.jsonl"))
    relations = list(iter_jsonl(package_path, "graph/relations.jsonl"))

    id_map: dict[str, str] = {}
    entity_rows: list[dict[str, Any]] = []
    for entity in entities:
        logical = str(entity.get("file_path") or "")
        target_abs = _target_abs(logical, deps=deps, target_library_id=target_library_id)
        new_node_id = idgen.recompute_entity_node_id(
            file_path=target_abs,
            entity_type=str(entity.get("entity_type") or ""),
            entity_name=str(entity.get("entity_name") or ""),
        )
        id_map[str(entity.get("node_id") or "")] = new_node_id
        updated = dict(entity)
        updated["workspace"] = target_library_id
        updated["file_path"] = target_abs
        updated["node_id"] = new_node_id
        entity_rows.append(updated)
    await neo4j.merge_entities(entity_rows)

    relation_rows: list[dict[str, Any]] = []
    for relation in relations:
        logical = str(relation.get("file_path") or "")
        target_abs = _target_abs(logical, deps=deps, target_library_id=target_library_id)
        new_relation_id = idgen.recompute_relation_id(
            file_path=target_abs,
            source_entity=str(relation.get("source_entity") or ""),
            target_entity=str(relation.get("target_entity") or ""),
        )
        updated = dict(relation)
        updated["workspace"] = target_library_id
        updated["file_path"] = target_abs
        updated["relation_id"] = new_relation_id
        updated["source_node_id"] = id_map.get(str(relation.get("source_node_id") or ""), "")
        updated["target_node_id"] = id_map.get(str(relation.get("target_node_id") or ""), "")
        relation_rows.append(updated)
    await neo4j.merge_relations(relation_rows)
