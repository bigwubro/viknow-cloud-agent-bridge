#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
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
    parser = argparse.ArgumentParser(description="Dedup absorption feasibility experiment")
    parser.add_argument("--dataset", default=None, help="JSON file path containing triples")
    parser.add_argument("--synthetic-total", type=int, default=5000, help="Synthetic triple count")
    parser.add_argument(
        "--duplicate-ratio", type=float, default=0.7, help="Synthetic duplicate ratio [0,1)"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for synthetic data")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _normalize_triple(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    src = str(item.get("src") or item.get("source") or item.get("src_id") or "").strip()
    rel = str(item.get("rel") or item.get("relation") or item.get("rel_type") or "").strip()
    tgt = str(item.get("tgt") or item.get("target") or item.get("tgt_id") or "").strip()
    doc_id = str(item.get("doc_id") or "").strip()
    chunk_id = str(item.get("chunk_id") or "").strip()
    if not src or not rel or not tgt:
        raise ValueError(f"invalid triple record: {item}")
    return src, rel, tgt, doc_id, chunk_id


def _load_triples(
    dataset_path: str | None, *, total: int, duplicate_ratio: float, seed: int
) -> list[tuple[str, str, str, str, str]]:
    if dataset_path:
        payload = load_json(dataset_path)
        if isinstance(payload, dict):
            records = payload.get("triples") or []
        elif isinstance(payload, list):
            records = payload
        else:
            raise ValueError("dataset must be list or {triples: [...]}")
        triples = [_normalize_triple(dict(item)) for item in records if isinstance(item, dict)]
        if not triples:
            raise ValueError("dataset has no valid triple records")
        return triples

    rnd = random.Random(seed)
    if duplicate_ratio < 0 or duplicate_ratio >= 1:
        raise ValueError("duplicate_ratio must be in [0, 1)")
    unique_count = max(1, int(total * (1 - duplicate_ratio)))
    base = [
        (
            f"entity_{i % 700}",
            f"relation_{i % 25}",
            f"entity_{(i * 3) % 700}",
            f"doc_{i % 200}",
            f"chunk_{i}",
        )
        for i in range(unique_count)
    ]
    triples = list(base)
    while len(triples) < total:
        src, rel, tgt, _, _ = rnd.choice(base)
        idx = len(triples)
        triples.append((src, rel, tgt, f"doc_dup_{idx % 100}", f"chunk_dup_{idx}"))
    rnd.shuffle(triples)
    return triples


def _evaluate(triples: list[tuple[str, str, str, str, str]]) -> dict[str, Any]:
    total = len(triples)
    unique_edge_keys = {(src, rel, tgt) for src, rel, tgt, _, _ in triples}
    unique_entities = {src for src, _, _, _, _ in triples} | {tgt for _, _, tgt, _, _ in triples}

    baseline_write_ops = total
    dedup_write_ops = len(unique_edge_keys)
    absorbed = baseline_write_ops - dedup_write_ops

    return {
        "input_triple_count": total,
        "unique_triple_count": len(unique_edge_keys),
        "unique_entity_count": len(unique_entities),
        "baseline_write_ops": baseline_write_ops,
        "dedup_write_ops": dedup_write_ops,
        "write_amplification_reduction": round(
            (baseline_write_ops - dedup_write_ops) / baseline_write_ops, 4
        )
        if baseline_write_ops
        else 0.0,
        "duplicate_absorb_rate": round(absorbed / baseline_write_ops, 4)
        if baseline_write_ops
        else 0.0,
        "unique_triples_ratio": round(len(unique_edge_keys) / baseline_write_ops, 4)
        if baseline_write_ops
        else 0.0,
    }


def main() -> int:
    args = parse_args()
    try:
        triples = _load_triples(
            args.dataset,
            total=max(1, args.synthetic_total),
            duplicate_ratio=float(args.duplicate_ratio),
            seed=args.seed,
        )
        result = _evaluate(triples)
        payload = {
            "experiment_name": "dedup_absorption",
            "generated_at": utc_timestamp(),
            "source": "dataset" if args.dataset else "synthetic",
            "metrics": result,
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logger.exception(f"dedup absorption experiment failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
