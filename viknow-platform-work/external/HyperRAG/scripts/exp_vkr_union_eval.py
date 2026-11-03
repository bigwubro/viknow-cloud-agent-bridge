#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from hyperrag.experiments.common import (
        latency_summary,
        load_json,
        save_json,
        utc_timestamp,
    )
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VKR runtime union experiment")
    parser.add_argument("--base-file", default=None, help="Base graph JSON path")
    parser.add_argument("--delta-file", default=None, help="Delta graph JSON path")
    parser.add_argument(
        "--synthetic-base", type=int, default=2000, help="Synthetic base edge count"
    )
    parser.add_argument(
        "--synthetic-delta", type=int, default=600, help="Synthetic delta edge count"
    )
    parser.add_argument(
        "--duplicate-ratio", type=float, default=0.35, help="Synthetic overlap ratio"
    )
    parser.add_argument("--runs", type=int, default=20, help="Merge repeat runs for latency")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _normalize_edge(item: dict[str, Any]) -> tuple[str, str, str]:
    src_id = str(item.get("src_id") or item.get("src") or "").strip()
    rel_type = str(item.get("rel_type") or item.get("rel") or "").strip()
    tgt_id = str(item.get("tgt_id") or item.get("tgt") or "").strip()
    if not src_id or not rel_type or not tgt_id:
        raise ValueError(f"invalid edge record: {item}")
    return src_id, rel_type, tgt_id


def _load_graph(path: str) -> list[tuple[str, str, str]]:
    payload = load_json(path)
    if isinstance(payload, dict):
        edges = payload.get("edges") or []
    elif isinstance(payload, list):
        edges = payload
    else:
        raise ValueError("graph payload must be list or {edges: [...]}")
    return [_normalize_edge(dict(edge)) for edge in edges if isinstance(edge, dict)]


def _build_synthetic(
    base_count: int, delta_count: int, duplicate_ratio: float, seed: int
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    rnd = random.Random(seed)
    base_edges = [
        (f"e{i % 1000}", f"r{i % 32}", f"e{(i * 7) % 1000}") for i in range(max(1, base_count))
    ]

    overlap = int(max(0, min(1, duplicate_ratio)) * delta_count)
    delta_edges = []
    for _ in range(max(0, overlap)):
        delta_edges.append(rnd.choice(base_edges))
    for i in range(max(0, delta_count - overlap)):
        idx = i + overlap
        delta_edges.append((f"e{(idx * 5) % 1300}", f"r{idx % 32}", f"e{(idx * 11) % 1300}"))
    rnd.shuffle(delta_edges)
    return base_edges, delta_edges


def _merge(
    base_edges: list[tuple[str, str, str]], delta_edges: list[tuple[str, str, str]]
) -> dict[str, Any]:
    base_set = set(base_edges)
    delta_set = set(delta_edges)
    union = base_set | delta_set
    overlap = base_set & delta_set
    return {
        "base_edge_count": len(base_edges),
        "delta_edge_count": len(delta_edges),
        "base_unique_edges": len(base_set),
        "delta_unique_edges": len(delta_set),
        "overlap_edges": len(overlap),
        "union_unique_edges": len(union),
        "dedup_ratio_in_delta": round(len(overlap) / len(delta_set), 4) if delta_set else 0.0,
    }


def main() -> int:
    args = parse_args()
    try:
        if args.base_file and args.delta_file:
            base_edges = _load_graph(args.base_file)
            delta_edges = _load_graph(args.delta_file)
            source = "dataset"
        else:
            base_edges, delta_edges = _build_synthetic(
                max(1, args.synthetic_base),
                max(1, args.synthetic_delta),
                float(args.duplicate_ratio),
                args.seed,
            )
            source = "synthetic"

        runs = max(1, int(args.runs))
        latencies = []
        merged_metrics: dict[str, Any] | None = None
        for _ in range(runs):
            start = time.perf_counter()
            merged_metrics = _merge(base_edges, delta_edges)
            latencies.append(time.perf_counter() - start)
        assert merged_metrics is not None

        payload = {
            "experiment_name": "vkr_union_eval",
            "generated_at": utc_timestamp(),
            "source": source,
            "metrics": merged_metrics,
            "merge_latency": latency_summary(latencies),
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logger.exception(f"vkr union experiment failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
