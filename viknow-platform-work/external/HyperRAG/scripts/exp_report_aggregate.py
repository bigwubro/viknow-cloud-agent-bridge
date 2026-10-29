#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from hyperrag.experiments.common import load_json, save_json, utc_timestamp
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.experiments.common import load_json, save_json, utc_timestamp  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate index optimization experiment reports")
    parser.add_argument("--inputs", nargs="+", required=True, help="Input report JSON files")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _pick(payload: dict[str, Any], path: list[str], default: Any = None) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def _extract_highlights(report: dict[str, Any]) -> dict[str, Any]:
    exp_name = str(report.get("experiment_name") or "unknown")
    if exp_name == "fast_ingest_benchmark":
        return {
            "throughput_docs_per_min": _pick(report, ["throughput_docs_per_min"], 0.0),
            "latency_p95_seconds": _pick(report, ["latency", "p95_seconds"], 0.0),
            "success_rate": _pick(report, ["success_rate"], 0.0),
        }
    if exp_name == "query_perf_quality":
        return {
            "idle_p95_seconds": _pick(report, ["idle_latency", "p95_seconds"], 0.0),
            "indexing_p95_seconds": _pick(
                report, ["indexing_phase", "latency", "p95_seconds"], None
            ),
            "quality_hit_rate": _pick(report, ["quality", "hit_rate"], None),
        }
    if exp_name == "dedup_absorption":
        return {
            "duplicate_absorb_rate": _pick(report, ["metrics", "duplicate_absorb_rate"], 0.0),
            "write_reduction": _pick(report, ["metrics", "write_amplification_reduction"], 0.0),
        }
    if exp_name == "vkr_union_eval":
        return {
            "overlap_edges": _pick(report, ["metrics", "overlap_edges"], 0),
            "merge_p95_seconds": _pick(report, ["merge_latency", "p95_seconds"], 0.0),
        }
    if exp_name == "post_compile_idempotency":
        return {
            "idempotency_pass": _pick(report, ["idempotency", "pass"], False),
            "guarded_p95_seconds": _pick(report, ["guarded_merge_latency", "p95_seconds"], 0.0),
        }
    if exp_name == "recovery_recompute":
        return {
            "recovery_success": _pick(report, ["metrics", "recovery_success"], False),
            "data_loss_rate": _pick(report, ["metrics", "data_loss_rate"], 1.0),
        }
    return {}


def main() -> int:
    args = parse_args()
    try:
        reports: list[dict[str, Any]] = []
        for input_path in args.inputs:
            payload = load_json(input_path)
            if not isinstance(payload, dict):
                raise ValueError(f"invalid report JSON object: {input_path}")
            payload["_path"] = str(Path(input_path).expanduser().resolve())
            reports.append(payload)

        highlights = []
        for report in reports:
            highlights.append(
                {
                    "experiment_name": str(report.get("experiment_name") or "unknown"),
                    "report_path": report["_path"],
                    "highlights": _extract_highlights(report),
                }
            )

        by_name = {item["experiment_name"]: item["highlights"] for item in highlights}
        go_nogo = {
            "throughput_latency_priority": {
                "has_fast_ingest": "fast_ingest_benchmark" in by_name,
                "has_query_perf": "query_perf_quality" in by_name,
                "fast_ingest_success_rate_ok": float(
                    by_name.get("fast_ingest_benchmark", {}).get("success_rate", 0.0)
                )
                >= 0.95,
                "query_idle_p95_available": by_name.get("query_perf_quality", {}).get(
                    "idle_p95_seconds"
                )
                is not None,
            }
        }

        payload = {
            "experiment_name": "report_aggregate",
            "generated_at": utc_timestamp(),
            "report_count": len(reports),
            "reports": highlights,
            "go_nogo": go_nogo,
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logger.exception(f"report aggregate failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
