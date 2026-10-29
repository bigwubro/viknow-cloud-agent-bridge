#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import os
import math
import time
import sys
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger
import yaml

try:
    from hyperrag.config_loader import HyperRAGConfig, load_config_from_yaml
    from hyperrag.single_layer_kg import SingleLayerKGService
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.config_loader import HyperRAGConfig, load_config_from_yaml  # noqa: E402
    from hyperrag.single_layer_kg import SingleLayerKGService  # noqa: E402


DEFAULT_APPLICATION_CONFIG = Path(__file__).resolve().parents[3] / "config" / "local" / "app.yaml"
DEFAULT_DATASET = Path(__file__).resolve().parent / "query_datasets" / "esp32_h2_single_layer_queries.json"


def _load_application_config(application_config_path: str | None) -> dict:
    path = Path(application_config_path or DEFAULT_APPLICATION_CONFIG)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    return payload if isinstance(payload, dict) else {}


def _apply_hyperrag_env(application_config: dict) -> None:
    hyperrag_env = (((application_config.get("hyperrag") or {}).get("env")) or {})
    if not isinstance(hyperrag_env, dict):
        return
    for key, value in hyperrag_env.items():
        os.environ[str(key)] = str(value)


def _normalize_mineru_lang(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    aliases = {
        "zh": "ch",
        "zh-cn": "ch",
        "zh_cn": "ch",
        "cn": "ch",
    }
    return aliases.get(normalized, normalized)


def _build_config_from_application(application_config: dict, workspace: str | None) -> HyperRAGConfig:
    hyperrag = application_config.get("hyperrag") or {}
    indexing = hyperrag.get("indexing") or {}
    params = indexing.get("params") or {}
    models = indexing.get("models") or {}
    llm = models.get("llm") or {}
    embedding = models.get("embedding") or {}
    mineru = models.get("mineru") or {}

    return HyperRAGConfig(
        workspace=workspace or "default",
        llm_model=str(llm.get("model") or "gpt-4o-mini"),
        llm_base_url=str(llm.get("base_url") or "https://api.openai.com/v1"),
        llm_api_key=str(llm.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""),
        embedding_model=str(embedding.get("model") or "text-embedding-3-small"),
        embedding_provider=str(embedding.get("provider") or "vllm"),
        embedding_base_url=str(embedding.get("base_url") or "https://api.openai.com/v1"),
        embedding_api_key=str(embedding.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""),
        embedding_dim=int((embedding.get("params") or {}).get("embedding_dim") or 1024),
        chunk_token_size=int(params.get("chunk_size") or 512),
        chunk_overlap_token_size=int(params.get("overlap_size") or 0),
        entity_types=list(params.get("entity_types") or []),
        enable_contextual_embeddings=bool(params.get("enable_contextual_embeddings") or False),
        mineru_backend=str(mineru.get("backend") or "vlm-http-client"),
        mineru_lang=_normalize_mineru_lang("zh"),
        kg_vector_enabled=True,
    )


def _build_experiment_config(args: argparse.Namespace) -> HyperRAGConfig:
    if args.config and Path(args.config).exists():
        config = load_config_from_yaml(args.config)
        if args.workspace:
            config.workspace = args.workspace
        return config
    application_config = _load_application_config(args.application_config)
    _apply_hyperrag_env(application_config)
    return _build_config_from_application(application_config, args.workspace)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate single-layer KG retrieval quality on a query dataset")
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=str(DEFAULT_APPLICATION_CONFIG), help="Fallback viknow application.yml path")
    parser.add_argument("--workspace", default=None, help="Override workspace")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Dataset JSON path")
    parser.add_argument("--file-path", default=None, help="Override document file path")
    parser.add_argument("--artifact-dir", default=None, help="Override artifact directory")
    parser.add_argument(
        "--query-scope",
        choices=["document", "workspace"],
        default="workspace",
        help="Run the legacy document query or the new workspace vector query",
    )
    parser.add_argument("--top-entities", type=int, default=10, help="Top entity cutoff")
    parser.add_argument("--top-relations", type=int, default=12, help="Top relation cutoff")
    parser.add_argument("--top-chunks", type=int, default=8, help="Top chunk cutoff")
    parser.add_argument("--case-limit", type=int, default=None, help="Only run the first N cases")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent query cases")
    parser.add_argument("--disable-llm-keywords", action="store_true", help="Disable LLM-based keyword extraction")
    parser.add_argument("--output-json", default=None, help="Optional output JSON path")
    return parser.parse_args()


def _load_dataset(dataset_path: str) -> Dict[str, Any]:
    payload = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Dataset must be a JSON object")
    cases = payload.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset cases must be a non-empty list")
    return payload


def _contains_keyword(text: str, keyword: str) -> bool:
    return keyword.strip().lower() in text.lower()


def _evaluate_case(case: Dict[str, Any], result: Dict[str, Any], top_entities: int) -> Dict[str, Any]:
    expected_entities = [str(item) for item in case.get("expected_entities") or [] if str(item).strip()]
    expected_chunk_keywords = [str(item) for item in case.get("expected_chunk_keywords") or [] if str(item).strip()]

    ranked_entities = [str(item.get("entity_name") or "") for item in result.get("entities") or []]
    entity_ranks: Dict[str, int | None] = {}
    for expected in expected_entities:
        entity_ranks[expected] = ranked_entities.index(expected) + 1 if expected in ranked_entities else None

    matched_entity_ranks = [rank for rank in entity_ranks.values() if rank is not None]
    entity_hit = bool(matched_entity_ranks)
    entity_recall = round(len(matched_entity_ranks) / max(1, len(expected_entities)), 3)
    entity_mrr = round(1.0 / min(matched_entity_ranks), 3) if matched_entity_ranks else 0.0

    chunk_texts = [str(item.get("text") or "") for item in result.get("chunks") or []]
    matched_chunk_keywords = [
        keyword
        for keyword in expected_chunk_keywords
        if any(_contains_keyword(text, keyword) for text in chunk_texts)
    ]
    chunk_hit = bool(matched_chunk_keywords)
    chunk_recall = round(len(matched_chunk_keywords) / max(1, len(expected_chunk_keywords)), 3)

    return {
        "id": case.get("id") or case.get("query"),
        "query": case.get("query"),
        "query_hit": entity_hit or chunk_hit,
        "entity_hit": entity_hit,
        "entity_recall": entity_recall,
        "entity_mrr": entity_mrr,
        "entity_ranks": entity_ranks,
        "chunk_hit": chunk_hit,
        "chunk_recall": chunk_recall,
        "matched_chunk_keywords": matched_chunk_keywords,
        "top_entity_names": ranked_entities[:top_entities],
        "top_chunk_ids": [str(item.get("chunk_id") or "") for item in result.get("chunks") or []],
    }


def _aggregate(case_reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    case_count = len(case_reports)
    if case_count == 0:
        return {
            "case_count": 0,
            "query_hit_count": 0,
            "query_hit_rate": 0.0,
            "entity_hit_rate": 0.0,
            "entity_avg_recall": 0.0,
            "entity_mrr": 0.0,
            "chunk_hit_rate": 0.0,
            "chunk_avg_recall": 0.0,
        }
    query_hit_count = sum(1 for item in case_reports if item["query_hit"])
    return {
        "case_count": case_count,
        "query_hit_count": query_hit_count,
        "query_hit_rate": round(query_hit_count / case_count, 3),
        "entity_hit_rate": round(sum(1 for item in case_reports if item["entity_hit"]) / case_count, 3),
        "entity_avg_recall": round(sum(item["entity_recall"] for item in case_reports) / case_count, 3),
        "entity_mrr": round(sum(item["entity_mrr"] for item in case_reports) / case_count, 3),
        "chunk_hit_rate": round(sum(1 for item in case_reports if item["chunk_hit"]) / case_count, 3),
        "chunk_avg_recall": round(sum(item["chunk_recall"] for item in case_reports) / case_count, 3),
    }


def _build_timing_summary(case_timings: List[float], wall_time: float) -> Dict[str, Any]:
    if not case_timings:
        return {
            "wall_time_seconds": round(wall_time, 3),
            "sum_case_time_seconds": 0.0,
            "avg_case_time_seconds": 0.0,
            "min_case_time_seconds": 0.0,
            "max_case_time_seconds": 0.0,
            "p50_case_time_seconds": 0.0,
            "p95_case_time_seconds": 0.0,
        }

    sorted_timings = sorted(case_timings)

    def _percentile(ratio: float) -> float:
        index = max(0, min(len(sorted_timings) - 1, math.ceil(len(sorted_timings) * ratio) - 1))
        return sorted_timings[index]

    return {
        "total_wall_time_seconds": round(wall_time, 3),
        "sum_case_time_seconds": round(sum(case_timings), 3),
        "avg_case_time_seconds": round(sum(case_timings) / len(case_timings), 3),
        "min_case_time_seconds": round(sorted_timings[0], 3),
        "max_case_time_seconds": round(sorted_timings[-1], 3),
        "p50_case_time_seconds": round(_percentile(0.5), 3),
        "p95_case_time_seconds": round(_percentile(0.95), 3),
    }


async def _query_case(
    experiment: SingleLayerKGService,
    *,
    args: argparse.Namespace,
    case: Dict[str, Any],
    file_path: str,
    artifact_dir: str,
) -> Dict[str, Any]:
    query = str(case.get("query") or "").strip()
    if not query:
        raise ValueError("Query text is empty")

    start = time.perf_counter()
    if args.query_scope == "workspace":
        result = await experiment.query_workspace_graph(
            query=query,
            top_entities=args.top_entities,
            top_relations=args.top_relations,
            top_chunks=args.top_chunks,
            use_llm_keywords=not args.disable_llm_keywords,
        )
    else:
        result = await experiment.query_document_graph(
            file_path=file_path,
            query=query,
            artifacts_dir=artifact_dir,
            top_entities=args.top_entities,
            top_relations=args.top_relations,
            top_chunks=args.top_chunks,
            use_llm_keywords=not args.disable_llm_keywords,
        )
    elapsed_seconds = time.perf_counter() - start
    report = _evaluate_case(case, result, args.top_entities)
    return {
        "id": report["id"],
        "query": query,
        "query_hit": report["query_hit"],
        "entity_hit": report["entity_hit"],
        "chunk_hit": report["chunk_hit"],
        "elapsed_seconds": round(elapsed_seconds, 3),
        "evaluation": report,
    }


async def _run(args: argparse.Namespace) -> int:
    dataset = _load_dataset(args.dataset)
    file_path = str(Path(args.file_path or dataset.get("document_file_path") or "").expanduser().resolve())
    artifact_dir = str(Path(args.artifact_dir or dataset.get("artifact_dir") or "").expanduser().resolve())
    if not file_path and args.query_scope == "document":
        raise ValueError("Document file path is required")
    if args.query_scope == "document" and not artifact_dir:
        raise ValueError("Artifact directory is required")

    cases = dataset.get("cases") or []
    if args.case_limit is not None:
        cases = cases[: max(0, args.case_limit)]

    config = _build_experiment_config(args)
    experiment = SingleLayerKGService(config)
    await experiment.initialize()

    try:
        concurrency = max(1, int(args.concurrency or 1))
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_case(case: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await _query_case(
                    experiment,
                    args=args,
                    case=case,
                    file_path=file_path,
                    artifact_dir=artifact_dir,
                )

        valid_cases = [case for case in cases if str(case.get("query") or "").strip()]
        wall_start = time.perf_counter()
        case_results = await asyncio.gather(*[_run_case(case) for case in valid_cases])
        wall_time = time.perf_counter() - wall_start

        case_reports = [item["evaluation"] for item in case_results]
        aggregate = _aggregate(case_reports)
        case_timings = [float(item["elapsed_seconds"]) for item in case_results]
        payload = {
            "dataset": str(Path(args.dataset).expanduser().resolve()),
            "workspace": config.workspace,
            "file_path": file_path,
            "artifact_dir": artifact_dir,
            "query_scope": args.query_scope,
            "concurrency": concurrency,
            "use_llm_keywords": not args.disable_llm_keywords,
            "hit_rate": {
                "case_count": aggregate["case_count"],
                "query_hit_count": aggregate["query_hit_count"],
                "query_hit_rate": aggregate["query_hit_rate"],
            },
            "timing": _build_timing_summary(case_timings, wall_time),
            "cases": [
                {
                    "id": item["id"],
                    "query": item["query"],
                    "hit": item["query_hit"],
                    "elapsed_seconds": item["elapsed_seconds"],
                }
                for item in case_results
            ],
        }

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output_json:
            Path(args.output_json).write_text(text, encoding="utf-8")
        print(text)
        return 0
    finally:
        await experiment.close()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"single_layer_kg_query_eval failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())