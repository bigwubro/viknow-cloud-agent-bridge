#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from hyperrag.experiments.common import latency_summary, save_json, utc_timestamp
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast ingest throughput and latency benchmark")
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=None, help="Fallback app yaml path")
    parser.add_argument("--workspace", default=None, help="Workspace override")
    parser.add_argument("--inputs", nargs="+", required=True, help="Input file paths")
    parser.add_argument(
        "--mode",
        default="job",
        choices=["job", "kg_direct"],
        help="job: full ingest job path, kg_direct: benchmark KG stage directly on preparsed text",
    )
    parser.add_argument("--concurrency", type=int, default=2, help="Max concurrent ingest jobs")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each input N times")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Per-job timeout")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _estimate_input_pages(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(str(path))
            pages = len(reader.pages)
            return pages if pages > 0 else 1
        except Exception:
            return 1

    if suffix in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        form_feed_pages = text.count("\f") + 1
        return max(1, form_feed_pages)

    return 1


def _build_preparsed_payload(
    path: Path, asset_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    blocks = [seg.strip() for seg in text.split("\n\n") if seg.strip()]
    if not blocks:
        blocks = [text or path.stem]

    segments_payload: list[dict[str, Any]] = []
    for i, block in enumerate(blocks):
        segments_payload.append(
            {
                "segment_id": f"{asset_id}-seg-{i}",
                "doc_id": asset_id,
                "order": i,
                "type": "text",
                "text": block,
                "location": {},
                "extra": {},
            }
        )

    asset_payload = {
        "doc_id": asset_id,
        "source_uri": str(path),
        "modality": "document",
        "segments_count": len(segments_payload),
        "title": path.stem,
        "language": "zh",
        "extra": {},
        "page_info": {},
    }
    return asset_payload, segments_payload


async def _run_one_kg_direct(
    runtime: ExperimentRuntime, workspace: str, input_path: str
) -> dict[str, Any]:
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    input_pages = _estimate_input_pages(path)

    asset_id = runtime.make_asset_id("exp-fast-ingest")
    asset_payload, segments_payload = _build_preparsed_payload(path, asset_id)
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

    started = time.perf_counter()
    try:
        result = await experiment.index_preparsed_document(
            file_path=str(path),
            asset_payload=asset_payload,
            segments_payload=segments_payload,
        )
        elapsed = time.perf_counter() - started
        timings = result.get("timings") if isinstance(result, dict) else {}
        counters = result.get("counters") if isinstance(result, dict) else {}
        kg_total_sec = (
            float(timings.get("total_sec"))
            if isinstance(timings, dict) and timings.get("total_sec")
            else None
        )
        kg_extract_sec = (
            float(timings.get("entity_relation_extraction_sec"))
            if isinstance(timings, dict) and timings.get("entity_relation_extraction_sec")
            else None
        )
        kg_chunks = (
            int(counters.get("chunks"))
            if isinstance(counters, dict) and counters.get("chunks")
            else None
        )
        return {
            "asset_id": asset_id,
            "job_id": None,
            "input": str(path),
            "input_pages": input_pages,
            "status": "completed",
            "elapsed_seconds": round(elapsed, 4),
            "kg_total_seconds": round(kg_total_sec, 4) if kg_total_sec is not None else None,
            "kg_entity_relation_extraction_seconds": round(kg_extract_sec, 4)
            if kg_extract_sec is not None
            else None,
            "kg_chunks": kg_chunks,
            "kg_chunks_per_second": round(kg_chunks / kg_extract_sec, 4)
            if isinstance(kg_chunks, int) and kg_extract_sec and kg_extract_sec > 0
            else None,
            "error": None,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return {
            "asset_id": asset_id,
            "job_id": None,
            "input": str(path),
            "input_pages": input_pages,
            "status": "failed",
            "elapsed_seconds": round(elapsed, 4),
            "kg_total_seconds": None,
            "kg_entity_relation_extraction_seconds": None,
            "kg_chunks": None,
            "kg_chunks_per_second": None,
            "error": str(exc),
        }


async def _run_one(
    runtime: ExperimentRuntime, workspace: str, input_path: str, timeout_s: int
) -> dict[str, Any]:
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    input_pages = _estimate_input_pages(path)

    asset_id = runtime.make_asset_id("exp-fast-ingest")
    start = time.perf_counter()
    job_id = await runtime.hyperrag.aadd_asset(
        workspace=workspace,
        asset_id=asset_id,
        asset_path=str(path),
        params={"language": "zh"},
    )
    job_status = await runtime.wait_job_terminal(job_id, timeout_s=timeout_s)
    elapsed = time.perf_counter() - start

    status = str(job_status.get("status") or "").lower()
    kg_task: dict[str, Any] | None = None
    for task_id in job_status.get("task_ids") or []:
        task = await runtime.hyperrag.aget_task_status(str(task_id))
        if isinstance(task, dict) and str(task.get("task_type") or "") == "kg_index":
            kg_task = task
            break

    kg_result = kg_task.get("result") if isinstance(kg_task, dict) else None
    kg_timings = kg_result.get("timings") if isinstance(kg_result, dict) else None
    kg_counters = kg_result.get("counters") if isinstance(kg_result, dict) else None
    kg_total_sec = (
        float(kg_timings.get("total_sec"))
        if isinstance(kg_timings, dict) and kg_timings.get("total_sec") is not None
        else None
    )
    kg_extract_sec = (
        float(kg_timings.get("entity_relation_extraction_sec"))
        if isinstance(kg_timings, dict)
        and kg_timings.get("entity_relation_extraction_sec") is not None
        else None
    )
    kg_chunks = (
        int(kg_counters.get("chunks"))
        if isinstance(kg_counters, dict) and kg_counters.get("chunks") is not None
        else None
    )
    kg_chunks_per_sec = (
        round(kg_chunks / kg_extract_sec, 4)
        if isinstance(kg_chunks, int) and kg_extract_sec and kg_extract_sec > 0
        else None
    )
    return {
        "asset_id": asset_id,
        "job_id": job_id,
        "input": str(path),
        "input_pages": input_pages,
        "status": status,
        "elapsed_seconds": round(elapsed, 4),
        "kg_total_seconds": round(kg_total_sec, 4) if kg_total_sec is not None else None,
        "kg_entity_relation_extraction_seconds": round(kg_extract_sec, 4)
        if kg_extract_sec is not None
        else None,
        "kg_chunks": kg_chunks,
        "kg_chunks_per_second": kg_chunks_per_sec,
        "error": str(job_status.get("error_message") or "") or None,
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
        targets = [str(Path(i).expanduser().resolve()) for i in args.inputs] * max(1, args.repeat)
        semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))

        async def _worker(path: str) -> dict[str, Any]:
            async with semaphore:
                if args.mode == "kg_direct":
                    return await _run_one_kg_direct(runtime, workspace, path)
                return await _run_one(runtime, workspace, path, args.timeout_seconds)

        wall_start = time.perf_counter()
        results = await asyncio.gather(*[_worker(path) for path in targets], return_exceptions=True)
        wall_elapsed = time.perf_counter() - wall_start

        records: list[dict[str, Any]] = []
        exceptions: list[str] = []
        errors: list[str] = []
        latencies: list[float] = []
        page_throughputs: list[float] = []
        kg_totals: list[float] = []
        kg_extracts: list[float] = []
        kg_chunk_speeds: list[float] = []
        for item in results:
            if isinstance(item, Exception):
                exceptions.append(str(item))
                continue
            records.append(item)
            if item["status"] == "completed":
                latencies.append(float(item["elapsed_seconds"]))
                item_pages = int(item.get("input_pages") or 1)
                item_elapsed = float(item["elapsed_seconds"])
                if item_elapsed > 0:
                    page_throughputs.append(item_pages / item_elapsed)
                kg_total = item.get("kg_total_seconds")
                if isinstance(kg_total, (int, float)):
                    kg_totals.append(float(kg_total))
                kg_extract = item.get("kg_entity_relation_extraction_seconds")
                if isinstance(kg_extract, (int, float)):
                    kg_extracts.append(float(kg_extract))
                kg_speed = item.get("kg_chunks_per_second")
                if isinstance(kg_speed, (int, float)):
                    kg_chunk_speeds.append(float(kg_speed))
            elif item.get("error"):
                errors.append(str(item["error"]))

        completed = sum(1 for item in records if item["status"] == "completed")
        failed = sum(1 for item in records if item["status"] == "failed") + len(exceptions)
        total = len(targets)
        total_pages = sum(int(item.get("input_pages") or 1) for item in records)

        payload = {
            "experiment_name": "fast_ingest_benchmark",
            "generated_at": utc_timestamp(),
            "workspace": workspace,
            "mode": args.mode,
            "config_path": args.config,
            "input_count": total,
            "input_pages": total_pages,
            "completed_count": completed,
            "failed_count": failed,
            "success_rate": round(completed / total, 4) if total else 0.0,
            "wall_time_seconds": round(wall_elapsed, 4),
            "throughput_pages_per_second": round(total_pages / wall_elapsed, 4)
            if wall_elapsed > 0
            else 0.0,
            "throughput_docs_per_min": round((completed / wall_elapsed) * 60, 4)
            if wall_elapsed > 0
            else 0.0,
            "latency": latency_summary(latencies),
            "page_throughput_per_job": latency_summary(page_throughputs),
            "fast_ingest_speed": {
                "kg_total_latency": latency_summary(kg_totals),
                "kg_entity_relation_extraction_latency": latency_summary(kg_extracts),
                "kg_chunk_throughput_chunks_per_sec": latency_summary(kg_chunk_speeds),
            },
            "records": records,
            "errors": [*errors, *exceptions],
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(payload if not args.output_json else f"saved report: {args.output_json}")
        return 0 if failed == 0 else 2
    finally:
        await runtime.shutdown()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"fast ingest benchmark failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
