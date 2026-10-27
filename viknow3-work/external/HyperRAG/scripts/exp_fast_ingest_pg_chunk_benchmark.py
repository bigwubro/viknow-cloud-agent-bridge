#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

try:
    import asyncpg
except ModuleNotFoundError:
    asyncpg = None  # type: ignore[assignment]

try:
    from hyperrag.experiments.common import latency_summary, save_json, utc_timestamp
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config
    from hyperrag.fast_ingest_runtime import FastIngestRelation, FastIngestRuntime
    from hyperrag.single_layer_kg import ChunkRecord
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.experiments.common import latency_summary, save_json, utc_timestamp  # noqa: E402
    from hyperrag.experiments.runtime import (  # noqa: E402
        ExperimentRuntime,
        build_experiment_config,
    )
    from hyperrag.fast_ingest_runtime import FastIngestRelation, FastIngestRuntime  # noqa: E402
    from hyperrag.single_layer_kg import ChunkRecord  # noqa: E402


@dataclass
class PgChunkRow:
    chunk_id: str
    full_doc_id: str
    chunk_order_index: int
    content: str
    tokens: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast ingest benchmark from PostgreSQL chunk pool")
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=None, help="Fallback app yaml path")
    parser.add_argument("--workspace", default=None, help="Workspace override")
    parser.add_argument(
        "--source-workspace",
        default="default",
        help="Workspace filter for hyperrag.hyperrag_bm25_chunks",
    )
    parser.add_argument(
        "--chunk-table",
        default="hyperrag.hyperrag_bm25_chunks",
        help="Postgres chunk table",
    )
    parser.add_argument("--chunk-limit", type=int, default=256, help="Sampled chunk count")
    parser.add_argument("--min-content-length", type=int, default=64, help="Min chunk text length")
    parser.add_argument(
        "--page-chunk-ratio",
        type=float,
        default=2.5,
        help="Approx conversion ratio: 1 page ~= N chunks",
    )
    parser.add_argument(
        "--er-extraction-max-async",
        type=int,
        default=None,
        help="Single run ER concurrency; ignored when --sweep-er-extraction-max-async is set",
    )
    parser.add_argument(
        "--sweep-er-extraction-max-async",
        default="",
        help="Comma-separated ER concurrency list, e.g. 16,32,64,96",
    )
    parser.add_argument(
        "--fast-ingest-retry-full-on-empty",
        choices=["auto", "true", "false"],
        default="auto",
        help="Override fast_ingest_er_retry_full_on_empty during benchmark",
    )
    parser.add_argument(
        "--fast-ingest-er-max-output-tokens",
        type=int,
        default=0,
        help="Override fast_ingest_er_max_output_tokens during benchmark (<=0 means no override)",
    )
    parser.add_argument(
        "--fast-ingest-er-description-max-chars",
        type=int,
        default=0,
        help="Override fast_ingest_er_description_max_chars during benchmark (<=0 means no override)",
    )
    parser.add_argument(
        "--with-dedup",
        action="store_true",
        help="Also execute FastIngestRuntime.ingest_relations for each chunk",
    )
    parser.add_argument(
        "--chunk-cache-path",
        default="",
        help="Chunk cache json path (avoid reading postgres every run)",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force refresh chunk cache from postgres",
    )
    parser.add_argument("--pg-host", default=os.environ.get("POSTGRES_HOST", "localhost"))
    parser.add_argument(
        "--pg-port", type=int, default=int(os.environ.get("POSTGRES_PORT", "15432"))
    )
    parser.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER", "postgres"))
    parser.add_argument("--pg-password", default=os.environ.get("POSTGRES_PASSWORD", ""))
    parser.add_argument("--pg-database", default=os.environ.get("POSTGRES_DATABASE", "viknow2"))
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


async def _load_chunks(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    table_name: str,
    source_workspace: str,
    chunk_limit: int,
    min_content_length: int,
) -> list[PgChunkRow]:
    if asyncpg is None:
        raise RuntimeError("asyncpg is required for PostgreSQL chunk benchmark")
    conn = await asyncpg.connect(
        user=user,
        password=password,
        database=database,
        host=host,
        port=port,
    )
    try:
        query = f"""
        SELECT id, full_doc_id, chunk_order_index, content, COALESCE(tokens, 0) AS tokens
        FROM {table_name}
        WHERE workspace = $1
          AND content IS NOT NULL
          AND length(trim(content)) >= $2
        ORDER BY RANDOM()
        LIMIT $3
        """
        rows = await conn.fetch(
            query,
            source_workspace,
            max(1, int(min_content_length)),
            max(1, int(chunk_limit)),
        )
        return [
            PgChunkRow(
                chunk_id=str(row["id"]),
                full_doc_id=str(row["full_doc_id"] or "unknown_doc"),
                chunk_order_index=int(row["chunk_order_index"] or 0),
                content=str(row["content"] or ""),
                tokens=int(row["tokens"] or 0),
            )
            for row in rows
        ]
    finally:
        await conn.close()


def _default_cache_path(args: argparse.Namespace) -> Path:
    cache_name = (
        "fast_ingest_chunks"
        f"_{args.source_workspace}"
        f"_{args.chunk_limit}"
        f"_{args.min_content_length}.json"
    )
    return Path("/home/zzz/viknow2/log/fast-ingest") / cache_name


def _save_chunk_cache(path: Path, rows: list[PgChunkRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "chunk_id": row.chunk_id,
            "full_doc_id": row.full_doc_id,
            "chunk_order_index": row.chunk_order_index,
            "content": row.content,
            "tokens": row.tokens,
        }
        for row in rows
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_chunk_cache(path: Path) -> list[PgChunkRow]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError(f"invalid chunk cache payload: {path}")
    rows: list[PgChunkRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            PgChunkRow(
                chunk_id=str(item.get("chunk_id") or ""),
                full_doc_id=str(item.get("full_doc_id") or "unknown_doc"),
                chunk_order_index=int(item.get("chunk_order_index") or 0),
                content=str(item.get("content") or ""),
                tokens=int(item.get("tokens") or 0),
            )
        )
    return [row for row in rows if row.chunk_id and row.content.strip()]


def _to_chunk_record(row: PgChunkRow) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=row.chunk_id,
        file_path=row.full_doc_id,
        order=row.chunk_order_index,
        raw_content=row.content,
        text=row.content,
        tokens=max(1, int(row.tokens or 0)),
        segment_ids=[row.chunk_id],
    )


async def _run_once(
    *,
    source_rows: list[PgChunkRow],
    runtime: ExperimentRuntime,
    workspace: str,
    max_async: int,
    with_dedup: bool,
    page_chunk_ratio: float,
) -> dict[str, Any]:
    use_fast_ingest_model = (
        runtime.config.fast_ingest_enabled
        and runtime.config.fast_ingest_decoding_strategy == "single_model"
        and bool(runtime.config.fast_ingest_model)
    )
    if use_fast_ingest_model:
        experiment = await runtime.hyperrag._get_kg_experiment(  # noqa: SLF001
            workspace,
            llm_model=runtime.config.fast_ingest_model,
            llm_base_url=runtime.config.fast_ingest_base_url,
            llm_api_key=runtime.config.fast_ingest_api_key,
        )
    else:
        experiment = await runtime.hyperrag._get_kg_experiment(workspace)  # noqa: SLF001

    experiment.config.ER_extraction_max_async = max(1, int(max_async))
    if getattr(experiment, "extractor", None) is not None:
        await experiment.extractor.close()  # type: ignore[attr-defined]
        experiment.extractor = None

    extractor = experiment._get_extractor()  # noqa: SLF001
    redis_client = runtime.hyperrag.scheduler._queue.redis  # noqa: SLF001
    fast_runtime = FastIngestRuntime(redis_client=redis_client)

    extract_latencies: list[float] = []
    ingest_latencies: list[float] = []
    relation_counts: list[int] = []
    entity_counts: list[int] = []
    failures: list[str] = []

    wall_started = time.perf_counter()

    async def _worker(row: PgChunkRow) -> dict[str, Any]:
        chunk = _to_chunk_record(row)
        t0 = time.perf_counter()
        entities, relations = await extractor.extract_chunk(
            chunk=chunk,
            modality="document",
            title=row.full_doc_id,
        )
        extract_elapsed = time.perf_counter() - t0

        ingest_elapsed = 0.0
        ingest_stats: dict[str, Any] | None = None
        if with_dedup and relations:
            t1 = time.perf_counter()
            ingest_stats = await fast_runtime.ingest_relations(
                workspace=workspace,
                doc_id=row.full_doc_id,
                relations=[
                    FastIngestRelation(
                        source_entity=rel.source_entity,
                        target_entity=rel.target_entity,
                        relation_type=rel.relationship_description,
                        relation_description=rel.situation or rel.relationship_description,
                        chunk_id=rel.chunk_id,
                    )
                    for rel in relations
                ],
                compile_phase=runtime.config.fast_ingest_compile_phase,
            )
            ingest_elapsed = time.perf_counter() - t1
        return {
            "chunk_id": row.chunk_id,
            "doc_id": row.full_doc_id,
            "extract_seconds": round(extract_elapsed, 4),
            "ingest_seconds": round(ingest_elapsed, 4),
            "entity_count": len(entities),
            "relation_count": len(relations),
            "ingest_stats": ingest_stats,
        }

    results = await asyncio.gather(*[_worker(row) for row in source_rows], return_exceptions=True)
    wall_elapsed = time.perf_counter() - wall_started

    records: list[dict[str, Any]] = []
    non_empty_entity_chunks = 0
    non_empty_relation_chunks = 0
    for item in results:
        if isinstance(item, Exception):
            failures.append(str(item))
            continue
        chunk_item = item
        records.append(chunk_item)
        extract_latencies.append(float(chunk_item["extract_seconds"]))
        ingest_latencies.append(float(chunk_item["ingest_seconds"]))
        relation_counts.append(int(chunk_item["relation_count"]))
        entity_counts.append(int(chunk_item["entity_count"]))
        if int(chunk_item["entity_count"]) > 0:
            non_empty_entity_chunks += 1
        if int(chunk_item["relation_count"]) > 0:
            non_empty_relation_chunks += 1

    processed_chunks = len(records)
    total_chunks = len(source_rows)
    pages_estimate = processed_chunks / max(0.0001, float(page_chunk_ratio))
    chunks_per_second = processed_chunks / wall_elapsed if wall_elapsed > 0 else 0.0
    pages_per_second = pages_estimate / wall_elapsed if wall_elapsed > 0 else 0.0
    extractor_diagnostics = (
        extractor.diagnostics_snapshot() if hasattr(extractor, "diagnostics_snapshot") else {}
    )
    return {
        "ER_extraction_max_async": int(max_async),
        "loaded_chunks": total_chunks,
        "processed_chunks": processed_chunks,
        "failed_chunks": len(failures),
        "page_chunk_ratio": float(page_chunk_ratio),
        "estimated_pages": round(pages_estimate, 4),
        "wall_time_seconds": round(wall_elapsed, 4),
        "throughput_chunks_per_second": round(chunks_per_second, 4),
        "throughput_pages_per_second": round(pages_per_second, 4),
        "extract_latency_per_chunk": latency_summary(extract_latencies),
        "ingest_latency_per_chunk": latency_summary(ingest_latencies),
        "entities_per_chunk": latency_summary(entity_counts),
        "relations_per_chunk": latency_summary(relation_counts),
        "non_empty_entity_chunk_rate": round(non_empty_entity_chunks / processed_chunks, 4)
        if processed_chunks
        else 0.0,
        "non_empty_relation_chunk_rate": round(non_empty_relation_chunks / processed_chunks, 4)
        if processed_chunks
        else 0.0,
        "extractor_diagnostics": extractor_diagnostics,
        "records": records[:50],
        "errors": failures,
    }


async def _run(args: argparse.Namespace) -> int:
    config = build_experiment_config(
        config_path=args.config,
        application_config_path=args.application_config,
        workspace=args.workspace,
    )
    if args.fast_ingest_retry_full_on_empty != "auto":
        config.fast_ingest_er_retry_full_on_empty = args.fast_ingest_retry_full_on_empty == "true"
    if int(args.fast_ingest_er_max_output_tokens or 0) > 0:
        config.fast_ingest_er_max_output_tokens = int(args.fast_ingest_er_max_output_tokens)
    if int(args.fast_ingest_er_description_max_chars or 0) > 0:
        config.fast_ingest_er_description_max_chars = int(args.fast_ingest_er_description_max_chars)
    workspace = args.workspace or config.workspace
    if not args.pg_password:
        raise RuntimeError(
            "pg password is required; provide --pg-password or set POSTGRES_PASSWORD"
        )

    cache_path = (
        Path(args.chunk_cache_path).expanduser()
        if args.chunk_cache_path
        else _default_cache_path(args)
    )
    if cache_path.exists() and not args.refresh_cache:
        source_rows = _load_chunk_cache(cache_path)
    else:
        source_rows = await _load_chunks(
            host=args.pg_host,
            port=int(args.pg_port),
            user=args.pg_user,
            password=args.pg_password,
            database=args.pg_database,
            table_name=args.chunk_table,
            source_workspace=args.source_workspace,
            chunk_limit=args.chunk_limit,
            min_content_length=args.min_content_length,
        )
        _save_chunk_cache(cache_path, source_rows)
    if not source_rows:
        raise RuntimeError("no chunk rows loaded from postgres")

    sweep_values: list[int] = []
    if args.sweep_er_extraction_max_async.strip():
        sweep_values.extend(
            int(item.strip())
            for item in args.sweep_er_extraction_max_async.split(",")
            if item.strip()
        )
    elif args.er_extraction_max_async is not None and args.er_extraction_max_async > 0:
        sweep_values.append(int(args.er_extraction_max_async))
    else:
        fast_ingest_model_active = (
            bool(config.fast_ingest_enabled)
            and str(config.fast_ingest_decoding_strategy or "").strip() == "single_model"
            and bool(str(config.fast_ingest_model or "").strip())
        )
        if fast_ingest_model_active:
            # Align with high-throughput vLLM service (max-num-seqs=512) while keeping benchmark cost bounded.
            auto_sweep = [96, 128, 192, 256, 384, 512]
            cap = max(1, int(args.chunk_limit or 256))
            sweep_values.extend(min(v, cap) for v in auto_sweep)
        else:
            sweep_values.append(config.ER_extraction_max_async)
    sweep_values = sorted(set(max(1, int(v)) for v in sweep_values))

    runtime = ExperimentRuntime(config)
    await runtime.initialize(ensure_rw=False)
    try:
        active_model = (
            str(runtime.config.fast_ingest_model or "")
            if (
                runtime.config.fast_ingest_enabled
                and runtime.config.fast_ingest_decoding_strategy == "single_model"
                and runtime.config.fast_ingest_model
            )
            else str(runtime.config.llm_model or "")
        )
        active_base_url = (
            str(runtime.config.fast_ingest_base_url or "")
            if (
                runtime.config.fast_ingest_enabled
                and runtime.config.fast_ingest_decoding_strategy == "single_model"
                and runtime.config.fast_ingest_base_url
            )
            else str(runtime.config.llm_base_url or "")
        )

        runs: list[dict[str, Any]] = []
        for max_async in sweep_values:
            run_payload = await _run_once(
                source_rows=source_rows,
                runtime=runtime,
                workspace=workspace,
                max_async=max_async,
                with_dedup=bool(args.with_dedup),
                page_chunk_ratio=float(args.page_chunk_ratio),
            )
            runs.append(run_payload)

        best_run = max(runs, key=lambda item: float(item.get("throughput_pages_per_second") or 0.0))
        payload = {
            "experiment_name": "fast_ingest_pg_chunk_benchmark",
            "generated_at": utc_timestamp(),
            "workspace": workspace,
            "source_workspace": args.source_workspace,
            "model": active_model,
            "llm_base_url": active_base_url,
            "with_dedup": bool(args.with_dedup),
            "chunk_table": args.chunk_table,
            "chunk_limit": int(args.chunk_limit),
            "chunk_cache_path": str(cache_path),
            "sweep_ER_extraction_max_async": sweep_values,
            "fast_ingest_er_max_output_tokens": int(
                runtime.config.fast_ingest_er_max_output_tokens
            ),
            "fast_ingest_er_description_max_chars": int(
                runtime.config.fast_ingest_er_description_max_chars
            ),
            "best_run": {
                "ER_extraction_max_async": int(best_run["ER_extraction_max_async"]),
                "throughput_pages_per_second": best_run["throughput_pages_per_second"],
                "throughput_chunks_per_second": best_run["throughput_chunks_per_second"],
                "wall_time_seconds": best_run["wall_time_seconds"],
                "extract_latency_avg_seconds": best_run["extract_latency_per_chunk"]["avg_seconds"],
                "extractor_diagnostics": best_run.get("extractor_diagnostics") or {},
            },
            "runs": runs,
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(payload if not args.output_json else f"saved report: {args.output_json}")
        has_failures = any(int(run.get("failed_chunks") or 0) > 0 for run in runs)
        return 0 if not has_failures else 2
    finally:
        await runtime.shutdown()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"fast ingest pg chunk benchmark failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
