#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
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
    parser = argparse.ArgumentParser(description="Post-compile idempotent merge experiment")
    parser.add_argument("--dataset", default=None, help="JSON file containing batch deltas")
    parser.add_argument("--synthetic-size", type=int, default=4000, help="Synthetic delta size")
    parser.add_argument("--replays", type=int, default=3, help="Replay count for the same batch")
    parser.add_argument(
        "--duplicate-ratio", type=float, default=0.3, help="Synthetic duplicate ratio"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _normalize_delta(item: dict[str, Any]) -> tuple[str, str, str, int, tuple[str, ...], str]:
    src = str(item.get("src_id") or "").strip()
    rel = str(item.get("rel_type") or item.get("rel") or "").strip()
    tgt = str(item.get("tgt_id") or "").strip()
    occ = int(item.get("occ_increment") or 1)
    refs_raw = item.get("new_refs") or item.get("source_refs") or []
    if not isinstance(refs_raw, list):
        refs_raw = [str(refs_raw)]
    refs = tuple(str(ref).strip() for ref in refs_raw if str(ref).strip())
    batch_id = str(item.get("batch_id") or "batch-1")
    if not src or not rel or not tgt:
        raise ValueError(f"invalid delta record: {item}")
    return src, rel, tgt, max(1, occ), refs, batch_id


def _load_deltas(
    dataset_path: str | None, *, synthetic_size: int, duplicate_ratio: float, seed: int
) -> list[tuple[str, str, str, int, tuple[str, ...], str]]:
    if dataset_path:
        payload = load_json(dataset_path)
        records = payload.get("batch_deltas") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise ValueError("dataset must be list or {batch_deltas: [...]}")
        return [_normalize_delta(dict(item)) for item in records if isinstance(item, dict)]

    rnd = random.Random(seed)
    total = max(1, synthetic_size)
    unique_count = max(1, int(total * (1 - max(0, min(0.95, duplicate_ratio)))))
    base = []
    for i in range(unique_count):
        base.append(
            (
                f"e{i % 1000}",
                f"r{i % 40}",
                f"e{(i * 13) % 1000}",
                (i % 3) + 1,
                (f"chunk_{i}",),
                "batch-1",
            )
        )
    deltas = list(base)
    while len(deltas) < total:
        src, rel, tgt, occ, refs, _ = rnd.choice(base)
        idx = len(deltas)
        deltas.append((src, rel, tgt, occ, refs + (f"chunk_dup_{idx}",), "batch-1"))
    rnd.shuffle(deltas)
    return deltas


def _simulate_merge(
    deltas: list[tuple[str, str, str, int, tuple[str, ...], str]],
    *,
    replays: int,
    idempotent_guard: bool,
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[float]]:
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    grouped_by_batch: dict[str, list[tuple[str, str, str, int, tuple[str, ...], str]]] = (
        defaultdict(list)
    )
    for delta in deltas:
        grouped_by_batch[delta[5]].append(delta)
    applied_batches: set[str] = set()
    per_replay_elapsed: list[float] = []

    for _ in range(max(1, replays)):
        start = time.perf_counter()
        for batch_id, batch_rows in grouped_by_batch.items():
            if idempotent_guard and batch_id in applied_batches:
                continue
            for src, rel, tgt, occ, refs, _ in batch_rows:
                edge_key = (src, rel, tgt)
                edge = edges.setdefault(edge_key, {"occurrence": 0, "source_refs": []})
                edge["occurrence"] += int(occ)
                edge["source_refs"].extend(refs)
            if idempotent_guard:
                applied_batches.add(batch_id)
        per_replay_elapsed.append(time.perf_counter() - start)
    return edges, per_replay_elapsed


def main() -> int:
    args = parse_args()
    try:
        deltas = _load_deltas(
            args.dataset,
            synthetic_size=args.synthetic_size,
            duplicate_ratio=args.duplicate_ratio,
            seed=args.seed,
        )
        expected = defaultdict(int)
        for src, rel, tgt, occ, _, _ in deltas:
            expected[(src, rel, tgt)] += int(occ)

        guarded, guarded_lat = _simulate_merge(deltas, replays=args.replays, idempotent_guard=True)
        unguarded, unguarded_lat = _simulate_merge(
            deltas, replays=args.replays, idempotent_guard=False
        )

        idempotent_pass = True
        for key, expected_occ in expected.items():
            if int(guarded.get(key, {}).get("occurrence") or 0) != expected_occ:
                idempotent_pass = False
                break

        payload = {
            "experiment_name": "post_compile_idempotency",
            "generated_at": utc_timestamp(),
            "input_delta_count": len(deltas),
            "replays": max(1, args.replays),
            "idempotency": {
                "pass": idempotent_pass,
                "unique_edges": len(guarded),
                "expected_unique_edges": len(expected),
            },
            "guarded_merge_latency": latency_summary(guarded_lat),
            "unguarded_merge_latency": latency_summary(unguarded_lat),
            "occurrence_multiplier_without_guard": round(
                (
                    sum(int(v.get("occurrence") or 0) for v in unguarded.values())
                    / max(1, sum(int(v.get("occurrence") or 0) for v in guarded.values()))
                ),
                4,
            ),
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if idempotent_pass else 2
    except Exception as exc:
        logger.exception(f"post compile idempotency experiment failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
