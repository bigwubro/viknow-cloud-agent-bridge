#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from hyperrag.experiments.common import save_json, utc_timestamp
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config
    from hyperrag.fast_ingest_runtime import FastIngestRelation, FastIngestRuntime
    from hyperrag.single_layer_kg import ChunkRecord
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.experiments.common import save_json, utc_timestamp  # noqa: E402
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config  # noqa: E402
    from hyperrag.fast_ingest_runtime import FastIngestRelation, FastIngestRuntime  # noqa: E402
    from hyperrag.single_layer_kg import ChunkRecord  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate post-compile merge to main KG and cleanup injected data"
    )
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=None, help="Fallback app yaml path")
    parser.add_argument("--workspace-prefix", default="post-compile-exp", help="Workspace prefix")
    parser.add_argument(
        "--chunk-cache-path",
        default="/home/zzz/viknow2/log/fast-ingest/fast_ingest_chunks_cache_512.json",
        help="Cached chunk dataset path",
    )
    parser.add_argument("--sample-size", type=int, default=96, help="Sample chunk count")
    parser.add_argument("--min-content-length", type=int, default=120, help="Min chunk text length")
    parser.add_argument(
        "--er-extraction-max-async", type=int, default=128, help="ER extraction concurrency"
    )
    parser.add_argument("--post-compile-batch-size", type=int, default=5000, help="Post compile batch size")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed")
    parser.add_argument(
        "--cleanup-after-validation",
        action="store_true",
        default=True,
        help="Clear post-compile injected KG data after validation",
    )
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _to_chunk_record(row: dict[str, Any]) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=str(row.get("chunk_id") or ""),
        file_path=str(row.get("full_doc_id") or "unknown_doc"),
        order=int(row.get("chunk_order_index") or 0),
        raw_content=str(row.get("content") or ""),
        text=str(row.get("content") or ""),
        tokens=max(1, int(row.get("tokens") or 0)),
        segment_ids=[str(row.get("chunk_id") or "")],
    )


async def _run(args: argparse.Namespace) -> int:
    config = build_experiment_config(
        config_path=args.config,
        application_config_path=args.application_config,
        workspace=None,
    )
    if not config.fast_ingest_enabled:
        raise RuntimeError("fast_ingest_enabled must be true for post-compile validation")
    if str(config.fast_ingest_decoding_strategy or "").strip() != "single_model":
        raise RuntimeError("fast_ingest_decoding_strategy must be single_model")
    if not str(config.fast_ingest_model or "").strip():
        raise RuntimeError("fast_ingest_model is required")

    rows = json.loads(Path(args.chunk_cache_path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid chunk cache payload: {args.chunk_cache_path}")
    candidates = [
        item
        for item in rows
        if isinstance(item, dict)
        and len(str(item.get("content") or "")) >= max(1, int(args.min_content_length))
        and str(item.get("chunk_id") or "").strip()
    ]
    if not candidates:
        raise RuntimeError("no usable chunks found in cache")
    rnd = random.Random(int(args.seed))
    rnd.shuffle(candidates)
    source_rows = candidates[: max(1, int(args.sample_size))]

    workspace = f"{args.workspace_prefix}-{int(time.time())}"
    runtime = ExperimentRuntime(config)
    await runtime.initialize(ensure_rw=False)
    try:
        experiment = await runtime.hyperrag._get_kg_experiment(  # noqa: SLF001
            workspace,
            llm_model=config.fast_ingest_model,
            llm_base_url=config.fast_ingest_base_url,
            llm_api_key=config.fast_ingest_api_key,
        )
        experiment.config.ER_extraction_max_async = max(1, int(args.er_extraction_max_async))
        if getattr(experiment, "extractor", None) is not None:
            await experiment.extractor.close()  # type: ignore[attr-defined]
            experiment.extractor = None
        extractor = experiment._get_extractor()  # noqa: SLF001
        fast_runtime = FastIngestRuntime(runtime.hyperrag.scheduler._queue.redis)  # noqa: SLF001

        started = time.perf_counter()
        extract_failures = 0
        extracted_relation_count = 0
        per_doc_relations: dict[str, list[FastIngestRelation]] = defaultdict(list)

        semaphore = asyncio.Semaphore(max(1, int(args.er_extraction_max_async)))

        async def _worker(row: dict[str, Any]) -> None:
            nonlocal extract_failures, extracted_relation_count
            async with semaphore:
                chunk = _to_chunk_record(row)
                try:
                    _, relations = await extractor.extract_chunk(
                        chunk=chunk,
                        modality="document",
                        title=str(row.get("full_doc_id") or "unknown_doc"),
                    )
                except Exception:
                    extract_failures += 1
                    return
                if not relations:
                    return
                extracted_relation_count += len(relations)
                per_doc_relations[str(row.get("full_doc_id") or "unknown_doc")].extend(
                    [
                        FastIngestRelation(
                            source_entity=rel.source_entity,
                            target_entity=rel.target_entity,
                            relation_type=rel.relationship_description,
                            relation_description=rel.situation or rel.relationship_description,
                            chunk_id=rel.chunk_id,
                        )
                        for rel in relations
                    ]
                )

        await asyncio.gather(*[_worker(row) for row in source_rows])
        extract_elapsed = time.perf_counter() - started

        ingest_stats: list[dict[str, Any]] = []
        for doc_id, relations in per_doc_relations.items():
            ingest_stats.append(
                await fast_runtime.ingest_relations(
                    workspace=workspace,
                    doc_id=doc_id,
                    relations=relations,
                    compile_phase=str(config.fast_ingest_compile_phase or "FAST_INGEST"),
                )
            )

        edge_key = fast_runtime._edge_map_key(workspace)  # noqa: SLF001
        edge_map_before = await runtime.hyperrag.scheduler._queue.redis.hgetall(edge_key)  # noqa: SLF001

        post_compile_result = await runtime.hyperrag.apost_compile_batch(
            workspace=workspace,
            batch_size=int(args.post_compile_batch_size),
        )
        kg_summary_after_post_compile = await runtime.hyperrag.ashow_post_compile_summary(
            workspace=workspace
        )
        edge_map_after = await runtime.hyperrag.scheduler._queue.redis.hgetall(edge_key)  # noqa: SLF001

        cleanup_result: dict[str, Any] | None = None
        kg_summary_after_cleanup: dict[str, Any] | None = None
        if bool(args.cleanup_after_validation):
            cleanup_result = await runtime.hyperrag.aclear_post_compile_injected(workspace=workspace)
            kg_summary_after_cleanup = await runtime.hyperrag.ashow_post_compile_summary(
                workspace=workspace
            )

        payload = {
            "experiment_name": "post_compile_pipeline_validation",
            "generated_at": utc_timestamp(),
            "workspace": workspace,
            "chunk_cache_path": str(Path(args.chunk_cache_path).expanduser()),
            "sample_size": len(source_rows),
            "extract_elapsed_seconds": round(extract_elapsed, 4),
            "extract_failures": int(extract_failures),
            "extracted_relation_count": int(extracted_relation_count),
            "hot_buffer_ingest_docs": len(per_doc_relations),
            "hot_buffer_edge_count_before_post_compile": len(edge_map_before),
            "hot_buffer_edge_count_after_post_compile": len(edge_map_after),
            "ingest_stats": ingest_stats[:20],
            "post_compile_result": post_compile_result,
            "kg_summary_after_post_compile": kg_summary_after_post_compile,
            "cleanup_after_validation": bool(args.cleanup_after_validation),
            "cleanup_result": cleanup_result,
            "kg_summary_after_cleanup": kg_summary_after_cleanup,
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(payload if not args.output_json else f"saved report: {args.output_json}")
        return 0
    finally:
        await runtime.shutdown()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"post compile pipeline validation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
