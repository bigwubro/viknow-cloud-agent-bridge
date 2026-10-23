#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from hyperrag.experiments.common import latency_summary, load_json, save_json, utc_timestamp
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.experiments.common import (  # noqa: E402
        latency_summary,
        load_json,
        save_json,
        utc_timestamp,
    )
    from hyperrag.experiments.runtime import (  # noqa: E402
        ExperimentRuntime,
        build_experiment_config,
    )


DEFAULT_QUESTIONS = [
    "这份文档讲了什么？",
    "请给出一个简短总结。",
    "文档中有哪些关键概念？",
    "请提取主要结论。",
    "文档里有哪些组件或步骤？",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query latency and retrieval-quality experiment")
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=None, help="Fallback app yaml path")
    parser.add_argument("--workspace", default=None, help="Workspace override")
    parser.add_argument(
        "--warmup-input", required=True, help="Input file used to build queryable baseline"
    )
    parser.add_argument(
        "--indexing-input", default=None, help="Optional second input for indexing-under-load run"
    )
    parser.add_argument("--total-queries", type=int, default=100, help="Total queries per phase")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent query workers")
    parser.add_argument("--query-mode", default="hybrid", help="Native retrieval mode")
    parser.add_argument("--retrieval-mode", default="hybrid", choices=["hybrid", "vector", "bm25"])
    parser.add_argument("--chunk-top-k", type=int, default=8, help="chunk_top_k for aquery_data")
    parser.add_argument(
        "--quality-dataset", default=None, help="JSON dataset path for hit-rate evaluation"
    )
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


async def _wait_completed(
    runtime: ExperimentRuntime, workspace: str, file_path: str, asset_prefix: str
) -> str:
    asset_id = runtime.make_asset_id(asset_prefix)
    job_id = await runtime.hyperrag.aadd_asset(
        workspace=workspace,
        asset_id=asset_id,
        asset_path=str(Path(file_path).expanduser().resolve()),
        params={"language": "zh"},
    )
    status = await runtime.wait_job_terminal(job_id, timeout_s=1800)
    if str(status.get("status") or "").lower() != "completed":
        raise RuntimeError(f"indexing failed: job_id={job_id}, status={status}")
    return job_id


async def _run_query_burst(
    runtime: ExperimentRuntime,
    *,
    workspace: str,
    total: int,
    concurrency: int,
    mode: str,
    retrieval_mode: str,
    chunk_top_k: int,
) -> list[float]:
    rnd = random.Random(42)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one() -> float:
        question = rnd.choice(DEFAULT_QUESTIONS)
        async with semaphore:
            start = time.perf_counter()
            await runtime.hyperrag.aquery_data(
                workspace=workspace,
                query=question,
                mode=mode,
                retrieval_mode=retrieval_mode,
                chunk_top_k=chunk_top_k,
            )
            return time.perf_counter() - start

    return await asyncio.gather(*[_one() for _ in range(total)])


def _contains_keyword(chunks: list[dict[str, Any]], keyword: str) -> bool:
    needle = keyword.strip().lower()
    if not needle:
        return False
    for chunk in chunks:
        text = str(chunk.get("content") or "").lower()
        if needle in text:
            return True
    return False


async def _run_quality(
    runtime: ExperimentRuntime,
    *,
    workspace: str,
    dataset_path: str,
    mode: str,
    retrieval_mode: str,
    chunk_top_k: int,
) -> dict[str, Any]:
    dataset = load_json(dataset_path)
    if not isinstance(dataset, dict):
        raise ValueError("quality dataset must be a JSON object")
    cases = dataset.get("cases") or []
    if not isinstance(cases, list):
        raise ValueError("quality dataset cases must be a list")

    reports: list[dict[str, Any]] = []
    hit_count = 0
    for case in cases:
        query = str((case or {}).get("query") or "").strip()
        expected = [
            str(item).strip()
            for item in ((case or {}).get("expected_chunk_keywords") or [])
            if str(item).strip()
        ]
        if not query:
            continue
        result = await runtime.hyperrag.aquery_data(
            workspace=workspace,
            query=query,
            mode=mode,
            retrieval_mode=retrieval_mode,
            chunk_top_k=chunk_top_k,
        )
        chunks = list(result.get("data", {}).get("chunks", []) or [])
        matched = [k for k in expected if _contains_keyword(chunks, k)]
        case_hit = bool(matched) if expected else bool(chunks)
        if case_hit:
            hit_count += 1
        reports.append(
            {
                "id": case.get("id") or query,
                "query": query,
                "expected_keyword_count": len(expected),
                "matched_keywords": matched,
                "hit": case_hit,
            }
        )

    total_cases = len(reports)
    return {
        "dataset": str(Path(dataset_path).expanduser().resolve()),
        "case_count": total_cases,
        "hit_count": hit_count,
        "hit_rate": round(hit_count / total_cases, 4) if total_cases else 0.0,
        "cases": reports,
    }


async def _run(args: argparse.Namespace) -> int:
    config = build_experiment_config(
        config_path=args.config,
        application_config_path=args.application_config,
        workspace=args.workspace,
    )
    workspace = args.workspace or config.workspace
    runtime = ExperimentRuntime(config)
    await runtime.initialize()

    try:
        warmup_job_id = await _wait_completed(
            runtime, workspace, args.warmup_input, "exp-query-warmup"
        )

        idle_latencies = await _run_query_burst(
            runtime,
            workspace=workspace,
            total=max(1, args.total_queries),
            concurrency=max(1, args.concurrency),
            mode=args.query_mode,
            retrieval_mode=args.retrieval_mode,
            chunk_top_k=max(1, args.chunk_top_k),
        )

        indexing_metrics: dict[str, Any] | None = None
        if args.indexing_input:
            indexing_asset_id = runtime.make_asset_id("exp-query-indexing")
            indexing_job_id = await runtime.hyperrag.aadd_asset(
                workspace=workspace,
                asset_id=indexing_asset_id,
                asset_path=str(Path(args.indexing_input).expanduser().resolve()),
                params={"language": "zh"},
            )
            await runtime.wait_index_task_running(indexing_job_id, timeout_s=900)
            indexing_latencies = await _run_query_burst(
                runtime,
                workspace=workspace,
                total=max(1, args.total_queries),
                concurrency=max(1, args.concurrency),
                mode=args.query_mode,
                retrieval_mode=args.retrieval_mode,
                chunk_top_k=max(1, args.chunk_top_k),
            )
            indexing_status = await runtime.wait_job_terminal(indexing_job_id, timeout_s=1800)
            indexing_metrics = {
                "job_id": indexing_job_id,
                "job_status": str(indexing_status.get("status") or "").lower(),
                "latency": latency_summary(indexing_latencies),
            }

        quality_metrics = None
        if args.quality_dataset:
            quality_metrics = await _run_quality(
                runtime,
                workspace=workspace,
                dataset_path=args.quality_dataset,
                mode=args.query_mode,
                retrieval_mode=args.retrieval_mode,
                chunk_top_k=max(1, args.chunk_top_k),
            )

        payload = {
            "experiment_name": "query_perf_quality",
            "generated_at": utc_timestamp(),
            "workspace": workspace,
            "warmup_job_id": warmup_job_id,
            "query_mode": args.query_mode,
            "retrieval_mode": args.retrieval_mode,
            "chunk_top_k": max(1, args.chunk_top_k),
            "idle_latency": latency_summary(idle_latencies),
            "indexing_phase": indexing_metrics,
            "quality": quality_metrics,
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        await runtime.shutdown()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"query perf/quality experiment failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
