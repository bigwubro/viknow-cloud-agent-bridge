"""Materialize Actio toolkit + model_calls for HyperRAG video indexing."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from viknow3.ops.config.runtime import RuntimeBundle

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _is_viknow3_repo_root(path: Path) -> bool:
    return (path / "src" / "viknow3").is_dir()


def resolve_viknow3_repo_root() -> Path | None:
    candidates: list[Path] = []
    for raw in (
        os.environ.get("VIKNOW3_ROOT", "").strip(),
        os.environ.get("VIKNOW_REPO_ROOT", "").strip(),
    ):
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.exists():
            candidates.append(candidate.resolve())
    hyperrag_raw = os.environ.get("VIKNOW_HYPERRAG_REPO_ROOT", "").strip()
    if hyperrag_raw:
        hyperrag = Path(hyperrag_raw).expanduser().resolve()
        candidates.extend([hyperrag.parent, hyperrag])
    here = Path(__file__).resolve()
    for depth in range(3, 8):
        if depth < len(here.parents):
            candidates.append(here.parents[depth])
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_viknow3_repo_root(resolved):
            return resolved
    return None


def resolve_actio_config_dir(repo_root: Path) -> Path | None:
    hyperrag_raw = os.environ.get("VIKNOW_HYPERRAG_REPO_ROOT", "").strip()
    candidates = [
        repo_root / "external" / "actio" / "configs",
    ]
    if hyperrag_raw:
        hyperrag = Path(hyperrag_raw).expanduser().resolve()
        candidates.extend(
            [
                hyperrag.parent / "external" / "actio" / "configs",
                hyperrag / "external" / "actio" / "configs",
            ]
        )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _integration_yaml_path(repo_root: Path) -> Path | None:
    for relative in ("config/actio.yaml", "config/demo/actio.yaml", "config/test/actio.yaml"):
        path = (repo_root / relative).resolve()
        if path.is_file():
            return path
    return None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected mapping in {path}")
    return loaded


def _write_yaml_mapping(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


def _actio_env_values(bundle: RuntimeBundle) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in os.environ.items():
        if value:
            values[key] = value
    actio = bundle.providers.get("actio") or {}
    base = str(actio.get("base_url") or values.get("ACTIO_SERVER_URL") or "http://127.0.0.1:8600").rstrip("/")
    values.setdefault("ACTIO_SERVER_URL", base)
    if values.get("ACTIO_API_KEY"):
        values.setdefault("ACTIO_API_KEY", values["ACTIO_API_KEY"])
    return values


def _expand_env_tokens(value: object, env: dict[str, str], *, field_name: str) -> object:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in env or not str(env[key]).strip():
                raise ValueError(f"missing env for {field_name}: {key}")
            return str(env[key])

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env_tokens(item, env, field_name=field_name) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _expand_env_tokens(item, env, field_name=f"{field_name}.{key}")
            for key, item in value.items()
        }
    return value




def _materialize_private_llm_base_url(ops_base: str, deploy_base: str) -> str:
    """Host playground: ops DB seeds http://127.0.0.1:8443; edge expects HTTPS."""
    deploy = str(deploy_base or "").strip().rstrip("/")
    ops = str(ops_base or "").strip().rstrip("/")
    env = os.environ.get("VIKNOW_PRIVATE_BASE_URL", "").strip().rstrip("/")
    candidate = ops or deploy or env
    if env and (
        not ops
        or "127.0.0.1" in ops
        or "localhost" in ops
        or (ops.startswith("http://") and ":8443" in ops)
    ):
        candidate = env
    elif candidate.startswith("http://") and ":8443" in candidate:
        if deploy.startswith("https://"):
            candidate = deploy
        else:
            candidate = "https://" + candidate.removeprefix("http://")
    return candidate.rstrip("/")


def _materialize_embedding_base_url(ops_base: str) -> str:
    env = os.environ.get("VIKNOW_EMBEDDING_BASE_URL", "").strip().rstrip("/")
    ops = str(ops_base or "").strip().rstrip("/")
    if env and (not ops or "127.0.0.1" in ops or "localhost" in ops):
        return env
    if ops:
        return ops
    return env or "http://ai.qingxiang.tech:8601/v1"


def _resolve_openai_defaults(bundle: RuntimeBundle) -> tuple[str, str, str]:
    from viknow3.ops.config.model_resolve import resolve_openai_chat
    from viknow3.ops.config.runtime_env import load_agent_runtime_env

    load_agent_runtime_env()
    private = bundle.providers.get("private") or {}
    chat = resolve_openai_chat("server-default")
    model = str(private.get("model_name") or chat.model or "").strip()
    env_model = os.environ.get("VIKNOW_PRIVATE_MODEL", "").strip()
    if env_model:
        model = env_model
    ops_base = str(private.get("base_url") or "").strip().rstrip("/")
    base = _materialize_private_llm_base_url(ops_base, chat.base_url)
    api_key = str(private.get("api_key") or chat.api_key or "").strip()
    return model, base, api_key


def _resolve_embedding_defaults(bundle: RuntimeBundle) -> tuple[str, str, str]:
    from viknow3.ops.config.model_resolve import resolve_openai_chat
    from viknow3.ops.config.runtime_env import load_agent_runtime_env
    from viknow3.ops.config.store import plain_provider

    load_agent_runtime_env()
    row = plain_provider("embedding") or bundle.providers.get("embedding") or {}
    model = str(row.get("model_name") or "Qwen/Qwen3-VL-Embedding-2B")
    if model == "Qwen3-VL-Embedding-2B":
        model = "Qwen/Qwen3-VL-Embedding-2B"
    base = str(row.get("base_url") or "").strip().rstrip("/")
    api_key = str(row.get("api_key") or "").strip()
    if not base or "127.0.0.1" in base or "localhost" in base:
        base = _materialize_embedding_base_url(base)
    if not api_key:
        api_key = str(row.get("api_key") or os.environ.get("VIKNOW_EMBEDDING_API_KEY") or "").strip()
        if not api_key:
            api_key = resolve_openai_chat("server-default").api_key
    return model, base, api_key


def materialize_actio_model_calls(state_dir: Path, bundle: RuntimeBundle) -> Path:
    llm_model, llm_base, llm_key = _resolve_openai_defaults(bundle)
    embed_model, embed_base, embed_key = _resolve_embedding_defaults(bundle)
    env = _actio_env_values(bundle)
    actio_base = env["ACTIO_SERVER_URL"].rstrip("/")
    actio_key = env.get("ACTIO_API_KEY", "")
    target = state_dir / "actio" / "model_calls.yaml"
    payload: dict[str, Any] = {
        "vlm": {
            "model_name": llm_model,
            "base_url": llm_base,
            "api_key": llm_key,
            "timeout_seconds": 600.0,
        },
        "llm": {
            "model_name": llm_model,
            "base_url": llm_base,
            "api_key": llm_key,
        },
        "embed": {
            "model_name": embed_model,
            "base_url": embed_base,
            "api_key": embed_key,
        },
        "asr": {
            "model_name": "Qwen/Qwen3-ASR-1.7B",
            "base_url": f"{actio_base}/asr/v1",
            "api_key": actio_key,
            "provider": "ark",
            "diarize": False,
            "num_speakers": None,
            "region_mode": "vad",
            "transport": "upload",
            "max_upload_mb": 128,
            "chunk_seconds": 1800,
            "job_timeout_seconds": 7200,
            "preparation_timeout_seconds": 1800,
            "queue_timeout_seconds": 1800,
            "transcription_timeout_seconds": 3600,
            "poll_interval_seconds": 2,
        },
        "ocr": {
            "model_name": "PaddleOCR",
            "base_url": f"{actio_base}/ocr/v1",
            "api_key": actio_key,
        },
    }
    _write_yaml_mapping(target, payload)
    return target


def _materialize_toolkit(state_dir: Path, integration: dict[str, Any], env: dict[str, str]) -> dict[str, Path]:
    toolkit = integration.get("toolkit")
    if not isinstance(toolkit, dict):
        raise ValueError("actio integration requires toolkit section")
    segmentation = toolkit.get("segmentation") or {}
    if not isinstance(segmentation, dict):
        raise ValueError("actio.toolkit.segmentation must be a mapping")
    selected = segmentation.get("selected")
    segment_payload = segmentation.get(selected) if selected else None
    if not isinstance(segment_payload, dict):
        raise ValueError("actio.toolkit requires selected segmentation payload")
    caption = toolkit.get("caption") or {}
    if not isinstance(caption, dict):
        raise ValueError("actio.toolkit.caption must be a mapping")

    toolkit_dir = state_dir / "actio" / "toolkit"
    segment_path = toolkit_dir / "segmentation.yml"
    aggregate_path = toolkit_dir / "aggregate.yml"
    merge_path = toolkit_dir / "caption_merge.yml"
    merge_dtw_path = toolkit_dir / "sop_alignment_dtw.yml"

    _write_yaml_mapping(
        segment_path,
        _expand_env_tokens(segment_payload, env, field_name="actio.toolkit.segmentation"),
    )
    _write_yaml_mapping(
        aggregate_path,
        _expand_env_tokens(dict(caption.get("aggregate") or {}), env, field_name="actio.toolkit.caption.aggregate"),
    )
    _write_yaml_mapping(
        merge_path,
        {
            "merge": _expand_env_tokens(
                dict((caption.get("sop_alignment") or {}).get("llm") or {}),
                env,
                field_name="actio.toolkit.caption.sop_alignment.llm",
            )
        },
    )
    _write_yaml_mapping(
        merge_dtw_path,
        {
            "merge": _expand_env_tokens(
                dict((caption.get("sop_alignment") or {}).get("dtw") or {}),
                env,
                field_name="actio.toolkit.caption.sop_alignment.dtw",
            )
        },
    )
    return {
        "segment_config": segment_path,
        "aggregate_config": aggregate_path,
        "merge_config": merge_path,
        "merge_dtw_config": merge_dtw_path,
    }


def build_actio_hyperrag_section(
    *,
    repo_root: Path,
    state_dir: Path,
    bundle: RuntimeBundle,
) -> tuple[dict[str, Any], str | None]:
    integration_path = _integration_yaml_path(repo_root)
    if integration_path is None:
        return {}, None

    integration = _load_yaml_mapping(integration_path)
    env = _actio_env_values(bundle)
    if not env.get("ACTIO_API_KEY", "").strip():
        raise RuntimeError(
            "ACTIO_API_KEY is required to materialize Actio video configs (set in deploy.env or the environment)."
        )

    models_config_path = materialize_actio_model_calls(state_dir, bundle)
    toolkit_paths = _materialize_toolkit(state_dir, integration, env)

    actio_payload: dict[str, Any] = {
        "models_config_path": str(models_config_path),
        "segment_config": str(toolkit_paths["segment_config"]),
        "aggregate_config": str(toolkit_paths["aggregate_config"]),
        "merge_config": str(toolkit_paths["merge_config"]),
        "merge_dtw_config": str(toolkit_paths["merge_dtw_config"]),
    }

    caption = (integration.get("toolkit") or {}).get("caption") or {}
    asr_caption = caption.get("asr") if isinstance(caption.get("asr"), dict) else {}
    vlm_caption = caption.get("vlm") if isinstance(caption.get("vlm"), dict) else {}
    if "max_concurrency" in asr_caption:
        actio_payload["asr_max_concurrency"] = max(1, int(asr_caption["max_concurrency"]))
    if "keep_segment_outputs" in asr_caption:
        actio_payload["keep_asr_segment_outputs"] = bool(asr_caption["keep_segment_outputs"])
    if "keep_audio_outputs" in asr_caption:
        actio_payload["keep_asr_audio_outputs"] = bool(asr_caption["keep_audio_outputs"])
    if "max_concurrency" in vlm_caption:
        actio_payload["annotate_max_concurrency"] = max(1, int(vlm_caption["max_concurrency"]))

    sop_schema = integration.get("sop_schema_path")
    if sop_schema:
        schema_path = (integration_path.parent / str(sop_schema)).resolve()
        if schema_path.is_file():
            actio_payload["sop_schema_path"] = str(schema_path)

    actio_config_dir = resolve_actio_config_dir(repo_root)
    return actio_payload, (str(actio_config_dir) if actio_config_dir else None)
