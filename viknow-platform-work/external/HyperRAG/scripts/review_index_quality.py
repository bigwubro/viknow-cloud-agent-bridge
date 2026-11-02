#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml
from loguru import logger

try:
    from hyperrag import HyperRAGV2, load_config_from_yaml
    from hyperrag.model_func import ImageCaptionFunc, LLMFunc, ModelConfig
    from hyperrag.review.batch_review import (
        BatchReviewItem,
        load_batch_review_items,
        render_batch_summary,
    )
    from hyperrag.review.reporting import write_report
    from hyperrag.review.reviewer import IndexQualityReviewer
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag import HyperRAGV2, load_config_from_yaml  # noqa: E402
    from hyperrag.model_func import ImageCaptionFunc, LLMFunc, ModelConfig  # noqa: E402
    from hyperrag.review.batch_review import (  # noqa: E402
        BatchReviewItem,
        load_batch_review_items,
        render_batch_summary,
    )
    from hyperrag.review.reporting import write_report  # noqa: E402
    from hyperrag.review.reviewer import IndexQualityReviewer  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review parse and chunk quality for an indexed asset"
    )
    parser.add_argument("--config", required=True, help="HyperRAG YAML configuration")
    workspace = parser.add_mutually_exclusive_group(required=True)
    workspace.add_argument("--workspace")
    workspace.add_argument("--library-id")
    parser.add_argument("--asset-id", default=None)
    parser.add_argument(
        "--csv",
        default=None,
        help="CSV file containing doc_id rows for batch review. Requires a doc_id column.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--app-config",
        default="/home/zzz/viknow2/config/local/app.yaml",
        help="ViKnow app.yaml used to resolve siliconflow judge config",
    )
    parser.add_argument(
        "--judge-source",
        choices=["siliconflow", "hyperrag"],
        default="siliconflow",
        help="Choose judge LLM source. siliconflow reads model/base_url/api_key from app.yaml+.env",
    )
    parser.add_argument(
        "--judge-model",
        default="Qwen/Qwen3.6-35B-A3B-FP8",
        help="Judge model label/ref/model name. Examples: Qwen/Qwen3.6-35B-A3B-FP8, GLM-5.2",
    )
    parser.add_argument("--judge-timeout", type=float, default=25.0)
    parser.add_argument("--judge-max-retries", type=int, default=0)
    parser.add_argument("--judge-max-tokens", type=int, default=512)
    parser.add_argument(
        "--vision-source",
        choices=["siliconflow", "hyperrag", "none"],
        default="hyperrag",
        help="Choose vision judge source. hyperrag uses runtime vision_model_func (configured in hyperrag.yml).",
    )
    parser.add_argument(
        "--vision-model",
        default="Qwen/Qwen3.6-35B-A3B-FP8",
        help="Vision model label/ref/model name. Example: Qwen/Qwen3.6-35B-A3B(SILICONFLOW)",
    )
    parser.add_argument("--vision-max-tokens", type=int, default=768)
    parser.add_argument("--parse-concurrency", type=int, default=8)
    parser.add_argument("--chunk-concurrency", type=int, default=16)
    parser.add_argument("--vision-concurrency", type=int, default=8)
    parser.add_argument(
        "--chunk-fallback-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "When chunk semantic judge fails (timeout/network/parser), "
            "fallback to boundary heuristic score instead of marking chunk unreviewed."
        ),
    )
    parser.add_argument(
        "--vision-sample-ratio",
        type=float,
        default=0.25,
        help="Fraction of PDF pages or PPT slides reviewed by the vision model",
    )
    args = parser.parse_args(argv)
    if not args.csv and not args.asset_id:
        parser.error("the following arguments are required: --asset-id")
    return args


def _default_batch_output_dir(csv_path: Path, workspace: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("artifacts/index_quality") / workspace / csv_path.stem / timestamp


def _expand_env(value):
    if isinstance(value, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _load_app_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        logger.warning(f"App config not found: {path}")
        return {}
    with p.open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    return _expand_env(payload)


def _resolve_siliconflow_judge(app_cfg: dict, selector: str) -> tuple[str, str, str] | None:
    model_providers = app_cfg.get("model_providers") or {}
    sf_provider = model_providers.get("siliconflow") or {}
    base_url_env = str(sf_provider.get("base_url_env") or "VIKNOW_SILICONFLOW_BASE_URL")
    api_key_env = str(sf_provider.get("api_key_env") or "VIKNOW_SILICONFLOW_API_KEY")
    base_url = os.environ.get(base_url_env, "")
    api_key = os.environ.get(api_key_env, "")
    if not base_url or not api_key:
        logger.warning(
            "Siliconflow env vars not ready: "
            f"base_url_env={base_url_env}, api_key_env={api_key_env}"
        )
        return None

    options = ((app_cfg.get("agent") or {}).get("model_options") or [])
    normalized_selector = selector.strip().lower()
    for option in options:
        if not isinstance(option, dict):
            continue
        ref = str(option.get("ref") or "")
        if not ref.startswith("siliconflow:"):
            continue
        label = str(option.get("label") or "")
        model_name = ref.split(":", 1)[1] if ":" in ref else ref
        candidates = {label.lower(), ref.lower(), model_name.lower()}
        if normalized_selector in candidates:
            return model_name, base_url, api_key

    # Fallback: allow direct model input even if not listed in app.yaml
    if selector:
        if selector.startswith("siliconflow:"):
            selector = selector.split(":", 1)[1]
        return selector, base_url, api_key
    return None


def _resolve_private_model(app_cfg: dict, selector: str | None = None) -> tuple[str, str, str] | None:
    model_providers = app_cfg.get("model_providers") or {}
    private_provider = model_providers.get("private") or {}
    base_url_env = str(private_provider.get("base_url_env") or "VIKNOW_PRIVATE_BASE_URL")
    api_key_env = str(private_provider.get("api_key_env") or "VIKNOW_PRIVATE_API_KEY")
    base_url = os.environ.get(base_url_env, "")
    api_key = os.environ.get(api_key_env, "")
    if not base_url or not api_key:
        logger.warning(
            "Private provider env vars not ready: "
            f"base_url_env={base_url_env}, api_key_env={api_key_env}"
        )
        return None

    env_model = os.environ.get("VIKNOW_PRIVATE_MODEL", "").strip()

    selected = (selector or "").strip()
    if selected:
        if selected.startswith("private:"):
            selected = selected.split(":", 1)[1]
        if env_model and (
            selected == env_model
            or selected.removeprefix("vllm/") == env_model.removeprefix("vllm/")
        ):
            return env_model, base_url, api_key
        return selected, base_url, api_key

    agent_cfg = app_cfg.get("agent") or {}
    model_cfg = agent_cfg.get("model") or {}
    ref = str(model_cfg.get("ref") or "")
    if ref.startswith("private:"):
        return ref.split(":", 1)[1], base_url, api_key

    if env_model:
        return env_model, base_url, api_key
    return None


def build_llm_func(config, args: argparse.Namespace):
    app_cfg = _load_app_config(args.app_config)
    if args.judge_source == "siliconflow":
        resolved = _resolve_siliconflow_judge(app_cfg, args.judge_model)
        if resolved is not None:
            model, base_url, api_key = resolved
            logger.info(
                "Using siliconflow judge "
                f"model={model}, timeout={args.judge_timeout}, retries={args.judge_max_retries}"
            )
            return LLMFunc(
                ModelConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    provider="vllm",
                    max_retries=max(0, args.judge_max_retries),
                    timeout=float(args.judge_timeout),
                ),
                max_tokens=max(64, args.judge_max_tokens),
                temperature=0,
            )
        logger.warning("Falling back to hyperrag llm config for judge")

    if args.judge_source == "hyperrag":
        resolved = _resolve_private_model(app_cfg, args.judge_model)
        if resolved is not None:
            model, base_url, api_key = resolved
            logger.info(
                "Using private judge model "
                f"model={model}, base_url={base_url}, timeout={args.judge_timeout}, retries={args.judge_max_retries}"
            )
            return LLMFunc(
                ModelConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    provider="vllm",
                    max_retries=max(0, args.judge_max_retries),
                    timeout=float(args.judge_timeout),
                ),
                max_tokens=max(64, args.judge_max_tokens),
                temperature=0,
            )
        logger.warning("Falling back to hyperrag llm config for judge")

    if callable(config.llm_model_func):
        return config.llm_model_func
    return LLMFunc(
        ModelConfig(
            base_url=config.llm_base_url,
            api_key=str(config.llm_api_key or "EMPTY"),
            model=config.llm_model,
            provider="vllm",
            max_retries=max(0, args.judge_max_retries),
            timeout=float(args.judge_timeout),
        ),
        max_tokens=max(64, args.judge_max_tokens),
        temperature=0,
    )


def build_vision_func(config, args: argparse.Namespace):
    app_cfg = _load_app_config(args.app_config)
    if args.vision_source == "none":
        logger.warning("Vision judge disabled by --vision-source none")
        return None

    if args.vision_source == "hyperrag" and callable(config.vision_model_func):
        logger.info("Using HyperRAG vision_model_func from runtime config")
        return config.vision_model_func

    if args.vision_source == "hyperrag":
        resolved = _resolve_private_model(app_cfg, args.vision_model)
        if resolved is not None:
            model, base_url, api_key = resolved
            logger.info(
                "Using private vision model "
                f"model={model}, base_url={base_url}, timeout={args.judge_timeout}"
            )
            return ImageCaptionFunc(
                ModelConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    provider="vllm",
                    max_retries=max(0, args.judge_max_retries),
                    timeout=float(args.judge_timeout),
                ),
                max_tokens=max(128, args.vision_max_tokens),
            )
        logger.warning("Unable to resolve private vision model from app config")

    if args.vision_source == "siliconflow":
        resolved = _resolve_siliconflow_judge(app_cfg, args.vision_model)
        if resolved is not None:
            model, base_url, api_key = resolved
            logger.info(
                "Using siliconflow vision judge "
                f"model={model}, timeout={args.judge_timeout}, retries={args.judge_max_retries}"
            )
            return ImageCaptionFunc(
                ModelConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    provider="vllm",
                    max_retries=max(0, args.judge_max_retries),
                    timeout=float(args.judge_timeout),
                ),
                max_tokens=max(128, args.vision_max_tokens),
            )
        logger.warning("Unable to resolve siliconflow vision model from app config")

    if callable(config.vision_model_func):
        logger.warning("Falling back to HyperRAG vision_model_func")
        return config.vision_model_func

    logger.warning("No vision model function is available; visual units will remain unreviewed")
    return None


def _load_hyperrag_config(config_path: str):
    """Load HyperRAG config with ${ENV_VAR} placeholders expanded from process env."""
    raw_path = Path(config_path).resolve()
    raw_text = raw_path.read_text(encoding="utf-8")
    expanded_text = _expand_env(raw_text)
    if expanded_text == raw_text:
        return load_config_from_yaml(str(raw_path)), None

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".expanded.yml",
        prefix="hyperrag-review-",
        dir=str(raw_path.parent),
        encoding="utf-8",
        delete=False,
    ) as temp_file:
        temp_file.write(expanded_text)
        temp_path = Path(temp_file.name)

    return load_config_from_yaml(str(temp_path)), temp_path


async def run(args: argparse.Namespace) -> tuple[Path, Path]:
    config, temp_config_path = _load_hyperrag_config(args.config)
    runtime = HyperRAGV2(config=config)
    workspace = str(args.workspace or args.library_id)
    try:
        reviewer = IndexQualityReviewer(
            runtime,
            build_llm_func(config, args),
            vision_func=build_vision_func(config, args),
            vision_sample_ratio=args.vision_sample_ratio,
            parse_concurrency=max(1, args.parse_concurrency),
            chunk_concurrency=max(1, args.chunk_concurrency),
            vision_concurrency=max(1, args.vision_concurrency),
            chunk_fallback_on_error=bool(args.chunk_fallback_on_error),
        )
        logger.info(
            "Review runtime profile: "
            f"judge_timeout={args.judge_timeout}s, judge_max_retries={args.judge_max_retries}, "
            f"parse_concurrency={args.parse_concurrency}, chunk_concurrency={args.chunk_concurrency}, "
            f"vision_concurrency={args.vision_concurrency}, vision_sample_ratio={args.vision_sample_ratio}, "
            f"chunk_fallback_on_error={args.chunk_fallback_on_error}"
        )
        if args.csv:
            batch_items = load_batch_review_items(Path(args.csv), default_workspace=workspace)
            reports = []
            for item in batch_items:
                report = await reviewer.review(workspace=item.workspace, asset_id=item.asset_id)
                reports.append(report)
            output_dir = Path(args.output_dir) if args.output_dir else _default_batch_output_dir(Path(args.csv), workspace)
            output_dir.mkdir(parents=True, exist_ok=True)
            aggregate_json = output_dir / "batch_report.json"
            aggregate_md = output_dir / "batch_report.md"
            aggregate_json.write_text(
                json.dumps([report.model_dump(mode="json") for report in reports], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            aggregate_md.write_text(render_batch_summary(reports, source_csv=Path(args.csv)), encoding="utf-8")
            return aggregate_json, aggregate_md

        output_dir = (
            Path(args.output_dir)
            if args.output_dir
            else (
                Path("artifacts/index_quality")
                / workspace
                / args.asset_id
                / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            )
        )
        report = await reviewer.review(workspace=workspace, asset_id=args.asset_id)
        return write_report(report, output_dir)
    finally:
        if temp_config_path is not None:
            try:
                temp_config_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(f"failed to cleanup temp config {temp_config_path}: {exc}")
        await runtime.shutdown()


def main() -> int:
    args = parse_args()
    try:
        json_path, markdown_path = asyncio.run(run(args))
        print(f"JSON report: {json_path}")
        print(f"Markdown report: {markdown_path}")
        return 0
    except Exception as exc:
        logger.exception(f"index quality review failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
