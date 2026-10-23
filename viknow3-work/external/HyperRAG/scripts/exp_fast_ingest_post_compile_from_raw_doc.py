#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from hyperrag.experiments.common import save_json, utc_timestamp
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.experiments.common import save_json, utc_timestamp  # noqa: E402
    from hyperrag.experiments.runtime import ExperimentRuntime, build_experiment_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate raw document -> fast_ingest route -> post_compile -> KG injection"
    )
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=None, help="Fallback app yaml path")
    parser.add_argument("--workspace-prefix", default="raw-fast-ingest-post-compile")
    parser.add_argument(
        "--input-file",
        default="/home/zzz/viknow2/external/HyperRAG/tests/integration/data/mini_lam.pdf",
        help="Input raw document path",
    )
    parser.add_argument("--language", default="zh", help="Parse language")
    parser.add_argument(
        "--parser-backend",
        default="pipeline",
        help="Parser backend for document parse stage (e.g. pipeline or vlm-http-client)",
    )
    parser.add_argument("--job-timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--post-compile-batch-size", type=int, default=5000)
    parser.add_argument("--clear-after-validation", action="store_true", default=True)
    parser.add_argument("--output-json", default=None)
    return parser.parse_args()


async def _wait_job_done(
    runtime: ExperimentRuntime,
    *,
    job_id: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        status = await runtime.hyperrag.aget_job_status(job_id)
        if status and status.get("status") in {"completed", "failed"}:
            return status
        await asyncio.sleep(max(0.2, poll_interval_seconds))
    raise TimeoutError(f"job timeout: {job_id}")


async def _run(args: argparse.Namespace) -> int:
    config = build_experiment_config(
        config_path=args.config,
        application_config_path=args.application_config,
        workspace=None,
    )
    input_file = Path(args.input_file).expanduser().resolve()
    if not input_file.exists():
        raise FileNotFoundError(f"input file not found: {input_file}")
    if not config.fast_ingest_enabled:
        raise RuntimeError("fast_ingest_enabled must be true")

    workspace = f"{args.workspace_prefix}-{int(time.time())}"
    asset_id = f"raw-doc-{int(time.time())}"

    runtime = ExperimentRuntime(config)
    await runtime.initialize(ensure_rw=False)
    try:
        job_id = await runtime.hyperrag.aadd_asset(
            workspace=workspace,
            asset_id=asset_id,
            asset_path=str(input_file),
            params={
                "language": str(args.language),
                "backend": str(args.parser_backend),
            },
            fast_ingest=True,
        )
        job_status = await _wait_job_done(
            runtime,
            job_id=job_id,
            timeout_seconds=max(60, int(args.job_timeout_seconds)),
            poll_interval_seconds=float(args.poll_interval_seconds),
        )
        if str(job_status.get("status")) != "completed":
            raise RuntimeError(f"index job failed: {job_status}")

        post_compile_result = await runtime.hyperrag.apost_compile_batch(
            workspace=workspace,
            batch_size=max(1, int(args.post_compile_batch_size)),
        )
        summary_after_post_compile = await runtime.hyperrag.ashow_post_compile_summary(
            workspace=workspace
        )

        clear_result: dict[str, Any] | None = None
        summary_after_clear: dict[str, Any] | None = None
        if bool(args.clear_after_validation):
            clear_result = await runtime.hyperrag.aclear_post_compile_injected(workspace=workspace)
            summary_after_clear = await runtime.hyperrag.ashow_post_compile_summary(workspace=workspace)

        payload = {
            "experiment_name": "raw_doc_fast_ingest_post_compile_validation",
            "generated_at": utc_timestamp(),
            "workspace": workspace,
            "asset_id": asset_id,
            "input_file": str(input_file),
            "job_id": job_id,
            "job_status": job_status.get("status"),
            "post_compile_result": post_compile_result,
            "summary_after_post_compile": summary_after_post_compile,
            "clear_after_validation": bool(args.clear_after_validation),
            "clear_result": clear_result,
            "summary_after_clear": summary_after_clear,
        }
        if args.output_json:
            save_json(args.output_json, payload)
            print(f"saved report: {args.output_json}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        await runtime.shutdown()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"raw fast-ingest post-compile validation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
