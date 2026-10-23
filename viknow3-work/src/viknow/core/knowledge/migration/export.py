"""Knowledge library export orchestration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viknow.core.config import (
    FILES_ROOT_KNOWLEDGE_DIR,
    FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR,
    FILES_ROOT_KNOWLEDGE_UPLOADS_DIR,
    resolve_knowledge_libraries_root,
    resolve_knowledge_uploads_root,
)
from viknow.core.knowledge.migration import idgen, pathmap
from viknow.core.knowledge.migration.models import (
    EmbeddingFingerprint,
    ManifestFileEntry,
    MigrationManifest,
    ProgressCallback,
    report_progress,
)
from viknow.core.knowledge.migration.package import build_package, compute_sha256
from viknow.core.knowledge.migration.storage import (
    MigrationNeo4j,
    MigrationPostgres,
    MigrationStorageConfig,
    build_video_segment_table_name,
    parse_vector_literal,
)
from viknow.core.knowledge.uploads.store import KnowledgeUploadStore


@dataclass
class ExporterDeps:
    files_root: Path
    storage_config: MigrationStorageConfig
    upload_store: KnowledgeUploadStore


class _Collector:
    """Streams rows into per-table lists and rewrites path fields to logical form."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def add(self, table: str, row: dict[str, Any]) -> None:
        self.rows.setdefault(table, []).append(row)

    def rewrite_path(self, value: str | None) -> str | None:
        if not value:
            return value
        return pathmap.to_logical_path(value) or value

    def counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.rows.items()}


def _rewrite_kg_row(
    row: dict[str, Any],
    *,
    collector: _Collector,
    recompute_id: bool,
) -> dict[str, Any]:
    """Rewrite a kg_*_vectors row into its logical form.

    ``file_path`` and nested ``metadata.file_path`` are rewritten to logical
    paths. When ``recompute_id`` is set the derived id (item_id / metadata id)
    is recomputed from the logical path so the package is self-consistent.
    """
    logical_path = collector.rewrite_path(row.get("file_path"))
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    updated = dict(row)
    updated["file_path"] = logical_path
    metadata = pathmap.rewrite_json_paths(metadata, rewrite=collector.rewrite_path)
    updated["metadata"] = metadata

    if recompute_id:
        if "entity_type" in metadata and "entity_name" in metadata:
            new_id = idgen.recompute_entity_node_id(
                file_path=logical_path or "",
                entity_type=str(metadata["entity_type"]),
                entity_name=str(metadata["entity_name"]),
            )
            updated["item_id"] = new_id
            metadata["node_id"] = new_id
        elif "source_entity" in metadata and "target_entity" in metadata:
            new_id = idgen.recompute_relation_id(
                file_path=logical_path or "",
                source_entity=str(metadata["source_entity"]),
                target_entity=str(metadata["target_entity"]),
            )
            updated["item_id"] = new_id
            metadata["relation_id"] = new_id
        elif metadata.get("order") is not None and "text" in metadata:
            new_id = idgen.recompute_chunk_id(
                file_path=logical_path or "",
                order=int(metadata["order"]),
                text=str(metadata["text"]),
            )
            updated["item_id"] = new_id
            metadata["chunk_id"] = new_id

    if "embedding" in updated and updated["embedding"] is not None:
        updated["embedding"] = parse_vector_literal(updated["embedding"])
    updated["metadata"] = metadata
    return updated


async def collect_files(*, files_root: Path, workspace: str) -> list[ManifestFileEntry]:
    """Walk uploads + working-dir roots, hashing every file into manifest entries."""
    return await asyncio.to_thread(_collect_files_sync, files_root=files_root, workspace=workspace)


def _collect_files_sync(*, files_root: Path, workspace: str) -> list[ManifestFileEntry]:
    entries: list[ManifestFileEntry] = []
    roots: list[tuple[Path, str]] = []

    uploads_root = resolve_knowledge_uploads_root(files_root) / workspace
    if uploads_root.is_dir():
        roots.append(
            (
                uploads_root,
                f"{FILES_ROOT_KNOWLEDGE_DIR}/{FILES_ROOT_KNOWLEDGE_UPLOADS_DIR}/{workspace}",
            )
        )

    libraries_root = resolve_knowledge_libraries_root(files_root) / workspace
    if libraries_root.is_dir():
        roots.append(
            (
                libraries_root,
                f"{FILES_ROOT_KNOWLEDGE_DIR}/{FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR}/{workspace}",
            )
        )

    for root, logical_prefix in roots:
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(root).as_posix()
            logical_path = f"{logical_prefix}/{relative}"
            entries.append(
                ManifestFileEntry(
                    logical_path=logical_path,
                    sha256=compute_sha256(file_path),
                    size_bytes=file_path.stat().st_size,
                )
            )
    return entries


async def export_library(
    *,
    deps: ExporterDeps,
    library_id: str,
    output_path: Path,
    progress: ProgressCallback | None = None,
) -> MigrationManifest:
    """Export one knowledge library into a single tarball package."""
    await report_progress(progress, "collect-files", 0.05, "收集文件清单")
    collector = _Collector()
    files = await collect_files(files_root=deps.files_root, workspace=library_id)

    pg = MigrationPostgres(deps.storage_config)
    neo4j = MigrationNeo4j(deps.storage_config)
    try:
        await report_progress(progress, "pg", 0.2, "导出向量/状态数据")
        await _export_pg(pg, library_id=library_id, collector=collector)
        await report_progress(progress, "graph", 0.5, "导出知识图谱")
        await _export_graph(neo4j, library_id=library_id, collector=collector)
    finally:
        await pg.close()
        await neo4j.close()

    await report_progress(progress, "uploads", 0.7, "导出上传记录")
    upload_records = await asyncio.to_thread(
        deps.upload_store.list_for_library,
        library_id=library_id,
        include_deleted=True,
    )
    upload_rows: list[dict[str, Any]] = []
    for record in upload_records:
        data = record.model_dump(mode="json")
        local_path = data.get("local_path")
        if local_path:
            data["local_path"] = pathmap.to_logical_path(local_path) or local_path
        upload_rows.append(data)
    for upload_row in upload_rows:
        collector.add("knowledge_uploads", upload_row)

    manifest = MigrationManifest(
        source_workspace=library_id,
        embedding=_build_fingerprint(deps.storage_config),
        files=files,
        table_counts=collector.counts(),
    )

    await report_progress(progress, "package", 0.85, "打包")
    await asyncio.to_thread(
        build_package,
        output_path=output_path,
        manifest=manifest,
        files_root=deps.files_root,
        pg_rows=collector.rows,
        graph_rows=_extract_graph_rows(collector),
    )
    await report_progress(progress, "done", 1.0, "导出完成")
    return manifest


def _extract_graph_rows(collector: _Collector) -> dict[str, list[dict[str, Any]]]:
    graph_rows: dict[str, list[dict[str, Any]]] = {}
    for name in ("entities", "relations"):
        rows = collector.rows.pop(f"graph_{name}", None)
        if rows is not None:
            graph_rows[name] = rows
    return graph_rows


async def _export_pg(
    pg: MigrationPostgres,
    *,
    library_id: str,
    collector: _Collector,
) -> None:
    config = pg.config
    kg_tables = {
        "kg_chunk_vectors": config.kg_chunk_table,
        "kg_entity_vectors": config.kg_entity_table,
        "kg_relation_vectors": config.kg_relation_table,
    }
    for output_name, table in kg_tables.items():
        rows = await pg.fetch_all(
            f"""
            SELECT workspace, item_id, file_path, content, embedding, metadata
            FROM {config.kg_schema}.{table}
            WHERE workspace = %s
            """,
            [library_id],
        )
        for row in rows:
            collector.add(
                output_name,
                _rewrite_kg_row(row, collector=collector, recompute_id=True),
            )

    asset_rows = await pg.fetch_all(
        """
        SELECT workspace, id, asset_id, status, created_at, updated_at, progress,
               error_message, parse_mode, file_path, metadata, file_data
        FROM public.hyperrag_asset_status
        WHERE workspace = %s
        """,
        [library_id],
    )
    for row in asset_rows:
        rewritten = dict(row)
        rewritten["file_path"] = collector.rewrite_path(row.get("file_path"))
        if isinstance(row.get("metadata"), (dict, str)):
            rewritten["metadata"] = pathmap.rewrite_json_paths(
                _maybe_json(row.get("metadata")), rewrite=collector.rewrite_path
            )
        if isinstance(row.get("file_data"), (dict, str)):
            rewritten["file_data"] = pathmap.rewrite_json_paths(
                _maybe_json(row.get("file_data")), rewrite=collector.rewrite_path
            )
        collector.add("asset_status", rewritten)

    bm25_rows = await pg.fetch_all(
        """
        SELECT workspace, id, full_doc_id, chunk_order_index, content,
               content_tokenized, tokens, created_at
        FROM public.hyperrag_bm25_chunks
        WHERE workspace = %s
        """,
        [library_id],
    )
    for row in bm25_rows:
        collector.add("bm25_chunks", row)

    video_table = build_video_segment_table_name(
        base_table=config.video_segment_table_base,
        model_name=config.video_embedding_model,
        vector_dim=config.video_embedding_dim,
    )
    try:
        video_rows = await pg.fetch_all(
            f"""
            SELECT workspace, id, video_name, segment_index, content_vector, metadata, created_at
            FROM public.{video_table}
            WHERE workspace = %s
            """,
            [library_id],
        )
    except Exception:
        video_rows = []
    for row in video_rows:
        updated = dict(row)
        updated["content_vector"] = parse_vector_literal(row.get("content_vector"))
        updated["metadata"] = pathmap.rewrite_json_paths(
            _maybe_json(row.get("metadata")), rewrite=collector.rewrite_path
        )
        collector.add("video_segments", updated)

    kb_rows = await pg.fetch_all(
        """
        SELECT * FROM public.hyperrag_knowledgebase_info
        WHERE id = %s
        """,
        [library_id],
    )
    for row in kb_rows:
        collector.add("knowledgebase_info", row)


async def _export_graph(
    neo4j: MigrationNeo4j,
    *,
    library_id: str,
    collector: _Collector,
) -> None:
    entities = await neo4j.fetch_entities(library_id)
    id_map: dict[str, str] = {}
    for entity in entities:
        logical_path = collector.rewrite_path(entity.get("file_path")) or ""
        new_node_id = idgen.recompute_entity_node_id(
            file_path=logical_path,
            entity_type=str(entity.get("entity_type") or ""),
            entity_name=str(entity.get("entity_name") or ""),
        )
        id_map[str(entity.get("node_id") or "")] = new_node_id
        updated = dict(entity)
        updated["file_path"] = logical_path
        updated["node_id"] = new_node_id
        collector.add("graph_entities", updated)

    relations = await neo4j.fetch_relations(library_id)
    for relation in relations:
        logical_path = (
            collector.rewrite_path((relation.get("relation") or {}).get("file_path")) or ""
        )
        relation_fields = dict(relation.get("relation") or {})
        new_relation_id = idgen.recompute_relation_id(
            file_path=logical_path,
            source_entity=str(relation_fields.get("source_entity") or ""),
            target_entity=str(relation_fields.get("target_entity") or ""),
        )
        updated = dict(relation_fields)
        updated["file_path"] = logical_path
        updated["relation_id"] = new_relation_id
        updated["source_node_id"] = id_map.get(str(relation.get("source_node_id") or ""), "")
        updated["target_node_id"] = id_map.get(str(relation.get("target_node_id") or ""), "")
        collector.add("graph_relations", updated)


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _build_fingerprint(config: MigrationStorageConfig) -> EmbeddingFingerprint:
    video_table = build_video_segment_table_name(
        base_table=config.video_segment_table_base,
        model_name=config.video_embedding_model,
        vector_dim=config.video_embedding_dim,
    )
    return EmbeddingFingerprint(
        embedding_dim=config.embedding_dim,
        video_embedding_model=config.video_embedding_model,
        video_embedding_dim=config.video_embedding_dim,
        video_segment_table=video_table,
    )
