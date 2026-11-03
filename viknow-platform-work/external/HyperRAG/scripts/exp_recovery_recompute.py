#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
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
    parser = argparse.ArgumentParser(description="Crash recovery and re-computation experiment")
    parser.add_argument("--manifest", default=None, help="Manifest JSON path")
    parser.add_argument("--synthetic-docs", type=int, default=200, help="Synthetic document count")
    parser.add_argument(
        "--chunks-per-doc", type=int, default=24, help="Synthetic chunks per document"
    )
    parser.add_argument("--archived-ratio", type=float, default=0.4, help="Archived document ratio")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--recompute-runs", type=int, default=3, help="Repeat recovery runs")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _hash_chunk(chunk_text: str) -> str:
    return hashlib.sha1(chunk_text.encode("utf-8")).hexdigest()[:16]


def _normalize_docs(payload: Any) -> list[dict[str, Any]]:
    docs = payload.get("docs") if isinstance(payload, dict) else payload
    if not isinstance(docs, list):
        raise ValueError("manifest must be list or {docs: [...]}")
    normalized: list[dict[str, Any]] = []
    for item in docs:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("doc_id") or "").strip()
        if not doc_id:
            continue
        chunks = [str(c) for c in (item.get("chunks") or [])]
        normalized.append(
            {
                "doc_id": doc_id,
                "chunks": chunks,
                "archived": bool(item.get("archived", False)),
            }
        )
    if not normalized:
        raise ValueError("manifest has no valid docs")
    return normalized


def _build_synthetic_docs(
    doc_count: int, chunks_per_doc: int, archived_ratio: float, seed: int
) -> list[dict[str, Any]]:
    rnd = random.Random(seed)
    docs: list[dict[str, Any]] = []
    for i in range(max(1, doc_count)):
        docs.append(
            {
                "doc_id": f"doc_{i:05d}",
                "chunks": [f"doc_{i}_chunk_{j}" for j in range(max(1, chunks_per_doc))],
                "archived": rnd.random() < max(0.0, min(1.0, archived_ratio)),
            }
        )
    return docs


def _extract_triples(docs: list[dict[str, Any]]) -> set[tuple[str, str]]:
    triples: set[tuple[str, str]] = set()
    for doc in docs:
        for chunk in doc["chunks"]:
            triples.add((doc["doc_id"], _hash_chunk(chunk)))
    return triples


def _simulate_recovery(docs: list[dict[str, Any]]) -> dict[str, Any]:
    archived_docs = [doc for doc in docs if doc["archived"]]
    pending_docs = [doc for doc in docs if not doc["archived"]]

    expected = _extract_triples(docs)
    persisted_after_crash = _extract_triples(archived_docs)
    recovered = set(persisted_after_crash)
    recovered.update(_extract_triples(pending_docs))

    missing = expected - recovered
    extra = recovered - expected

    return {
        "doc_count": len(docs),
        "archived_doc_count": len(archived_docs),
        "pending_doc_count": len(pending_docs),
        "expected_triple_count": len(expected),
        "persisted_after_crash_triple_count": len(persisted_after_crash),
        "recovered_triple_count": len(recovered),
        "missing_triples": len(missing),
        "unexpected_triples": len(extra),
        "recovery_success": len(missing) == 0 and len(extra) == 0,
        "data_loss_rate": round(len(missing) / max(1, len(expected)), 6),
    }


def main() -> int:
    args = parse_args()
    try:
        if args.manifest:
            docs = _normalize_docs(load_json(args.manifest))
            source = "manifest"
        else:
            docs = _build_synthetic_docs(
                doc_count=args.synthetic_docs,
                chunks_per_doc=args.chunks_per_doc,
                archived_ratio=args.archived_ratio,
                seed=args.seed,
            )
            source = "synthetic"

        run_count = max(1, int(args.recompute_runs))
        latencies = []
        final_metrics: dict[str, Any] | None = None
        for _ in range(run_count):
            start = time.perf_counter()
            final_metrics = _simulate_recovery(docs)
            latencies.append(time.perf_counter() - start)
        assert final_metrics is not None

        payload = {
            "experiment_name": "recovery_recompute",
            "generated_at": utc_timestamp(),
            "source": source,
            "runs": run_count,
            "metrics": final_metrics,
            "recompute_latency": latency_summary(latencies),
        }
        if args.output_json:
            save_json(args.output_json, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if bool(final_metrics.get("recovery_success")) else 2
    except Exception as exc:
        logger.exception(f"recovery recompute experiment failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
