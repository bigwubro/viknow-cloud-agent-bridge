#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from loguru import logger
import yaml

try:
    from hyperrag.config_loader import HyperRAGConfig, load_config_from_yaml
    from hyperrag.single_layer_kg import SingleLayerKGService, build_parser_params_from_args
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag.config_loader import HyperRAGConfig, load_config_from_yaml  # noqa: E402
    from hyperrag.single_layer_kg import SingleLayerKGService, build_parser_params_from_args  # noqa: E402


DEFAULT_APPLICATION_CONFIG = Path(__file__).resolve().parents[3] / "config" / "local" / "app.yaml"


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
    parser = argparse.ArgumentParser(
        description="Single-layer synonym knowledge graph experiment for HyperRAG",
    )
    parser.add_argument("--config", default="hyperrag.yml", help="HyperRAG YAML config path")
    parser.add_argument("--application-config", default=str(DEFAULT_APPLICATION_CONFIG), help="Fallback viknow application.yml path")
    parser.add_argument("--workspace", default=None, help="Override workspace")

    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Parse, chunk, extract and upsert one document graph")
    build.add_argument("--input", required=True, help="Input document path")
    build.add_argument("--mineru-dir", default=None, help="Optional MinerU cache dir")
    build.add_argument("--output-json", default=None, help="Optional JSON summary output path")
    build.add_argument("--artifacts-dir", default=None, help="Optional directory to store parsed chunks and extracted assertions")
    build.add_argument("--method", default=None, choices=["auto", "txt", "ocr"], help="MinerU parse method")
    build.add_argument("--lang", default=None, help="Document language")
    build.add_argument("--device", default=None, help="MinerU device")
    build.add_argument("--backend", default=None, help="MinerU backend")
    build.add_argument("--formula", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable formula parsing")
    build.add_argument("--table", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable table parsing")

    delete = subparsers.add_parser("delete", help="Delete one document graph")
    delete.add_argument("--file-path", required=True, help="Absolute or relative document file path")

    clear_workspace = subparsers.add_parser("clear-workspace", help="Delete all TLKG data in one workspace")
    clear_workspace.add_argument("--target-workspace", default=None, help="Workspace to clear; defaults to --workspace")

    clear_tests = subparsers.add_parser("clear-test-graphs", help="Delete all TLKG test data by workspace prefix")
    clear_tests.add_argument("--workspace-prefix", default="tlkg_", help="Workspace prefix to clear")

    show_doc = subparsers.add_parser("show-doc", help="Show one document graph summary")
    show_doc.add_argument("--file-path", required=True, help="Absolute or relative document file path")

    show_synonyms = subparsers.add_parser("show-synonyms", help="Show synonym relations")
    show_synonyms.add_argument("--file-path", default=None, help="Optional document file path filter")
    show_synonyms.add_argument("--limit", type=int, default=50, help="Max rows")

    subparsers.add_parser("show-graph-summary", help="Show graph summary")

    benchmark_pair = subparsers.add_parser("benchmark-pair", help="Insert two related documents and print combined performance stats")
    benchmark_pair.add_argument("--first-input", required=True, help="First document path")
    benchmark_pair.add_argument("--second-input", required=True, help="Second document path")
    benchmark_pair.add_argument("--mineru-dir", default=None, help="Optional MinerU cache dir")
    benchmark_pair.add_argument("--output-json", default=None, help="Optional JSON summary output path")
    benchmark_pair.add_argument("--artifacts-dir", default=None, help="Optional artifacts root dir")
    benchmark_pair.add_argument("--method", default=None, choices=["auto", "txt", "ocr"], help="MinerU parse method")
    benchmark_pair.add_argument("--lang", default=None, help="Document language")
    benchmark_pair.add_argument("--device", default=None, help="MinerU device")
    benchmark_pair.add_argument("--backend", default=None, help="MinerU backend")
    benchmark_pair.add_argument("--formula", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable formula parsing")
    benchmark_pair.add_argument("--table", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable table parsing")

    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    config = _build_experiment_config(args)
    experiment = SingleLayerKGService(config)
    await experiment.initialize()
    try:
        if args.command == "build":
            result = await experiment.build_document_graph(
                input_path=args.input,
                mineru_dir=args.mineru_dir,
                parser_params=build_parser_params_from_args(args),
                artifacts_dir=args.artifacts_dir,
            )
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output_json:
                Path(args.output_json).write_text(text, encoding="utf-8")
            print(text)
            return 0

        if args.command == "delete":
            result = await experiment.delete_document_graph(args.file_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "clear-workspace":
            result = await experiment.clear_workspace(workspace=args.target_workspace)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "clear-test-graphs":
            result = await experiment.clear_test_graphs(workspace_prefix=args.workspace_prefix)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-doc":
            result = await experiment.show_document_graph(args.file_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-synonyms":
            result = await experiment.show_synonyms(file_path=args.file_path, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-graph-summary":
            result = await experiment.show_graph_summary()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "benchmark-pair":
            first = await experiment.build_document_graph(
                input_path=args.first_input,
                mineru_dir=args.mineru_dir,
                parser_params=build_parser_params_from_args(args),
                artifacts_dir=args.artifacts_dir,
            )
            second = await experiment.build_document_graph(
                input_path=args.second_input,
                mineru_dir=args.mineru_dir,
                parser_params=build_parser_params_from_args(args),
                artifacts_dir=args.artifacts_dir,
            )
            graph_summary = await experiment.show_graph_summary()
            synonym_summary = await experiment.show_synonyms(limit=50)
            result = {
                "first": first,
                "second": second,
                "graph_summary": graph_summary,
                "synonym_summary": synonym_summary,
            }
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output_json:
                Path(args.output_json).write_text(text, encoding="utf-8")
            print(text)
            return 0

        raise ValueError(f"Unsupported command: {args.command}")
    finally:
        await experiment.close()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.exception(f"single_layer_kg service CLI failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())