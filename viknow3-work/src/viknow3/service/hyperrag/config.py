"""HyperRAG-facing config — sourced from viknow_ops, not repo YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from viknow3.ops.config.runtime import load_runtime_bundle
from viknow3.ops.config.store import configured


@dataclass(frozen=True)
class LangfuseConfig:
    enabled: bool = False
    index_enabled: bool | None = None


@dataclass(frozen=True)
class ObservabilityConfig:
    langfuse: LangfuseConfig


@dataclass(frozen=True)
class SchedulerUnifiedConfig:
    redis_url: str


@dataclass(frozen=True)
class SchedulerConfig:
    unified: SchedulerUnifiedConfig


@dataclass(frozen=True)
class AppSettings:
    state_dir: Path


@dataclass(frozen=True)
class HyperRAGBackendConfig:
    config_path: Path
    repo_root: Path | None
    runtime_concurrency: int = 64
    sync_bridge_concurrency: int = 64
    default_query_mode: str = "hybrid"
    default_top_k: int = 10


@dataclass(frozen=True)
class KnowledgeConfig:
    default_library_id: str
    hyperrag: HyperRAGBackendConfig


@dataclass(frozen=True)
class AppConfig:
    app: AppSettings
    knowledge: KnowledgeConfig
    scheduler: SchedulerConfig
    observability: ObservabilityConfig


def _hyperrag_from_repo_root(repo_root: Path) -> Path | None:
    candidate = (repo_root / "external" / "HyperRAG").resolve()
    if (candidate / "src" / "hyperrag" / "config_loader.py").is_file():
        return candidate
    return None


def _find_hyperrag_repo_root() -> Path | None:
    candidates: list[Path] = []
    for raw in (
        os.environ.get("VIKNOW_HYPERRAG_REPO_ROOT", "").strip(),
        os.environ.get("VIKNOW3_ROOT", "").strip(),
        os.environ.get("VIKNOW_REPO_ROOT", "").strip(),
    ):
        if raw:
            path = Path(raw).expanduser()
            if path.exists():
                candidates.append(path.resolve())
    candidates.append(Path(__file__).resolve().parents[4])
    seen: set[Path] = set()
    for repo_root in candidates:
        if repo_root in seen:
            continue
        seen.add(repo_root)
        hyperrag = _hyperrag_from_repo_root(repo_root)
        if hyperrag is not None:
            return hyperrag
        if (repo_root / "src" / "hyperrag" / "config_loader.py").is_file():
            return repo_root
    return None


def load_app_config() -> AppConfig:
    if not configured():
        raise RuntimeError(
            "VIKNOW_OPS_DSN is required. HyperRAG reads connections and models from viknow_ops, not YAML."
        )

    bundle = load_runtime_bundle()
    repo_root = _find_hyperrag_repo_root()
    state_dir = Path(os.environ.get("VIKNOW_STATE_DIR", "/tmp/viknow3-state")).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    materialized = state_dir / "hyperrag" / "hyperrag.yml"

    retrieval = bundle.retrieval
    default_library = str(retrieval.get("library") or "default")
    default_mode = str(retrieval.get("mode") or "hybrid")
    default_top_k = int(retrieval.get("top_k") or 10)

    backend = HyperRAGBackendConfig(
        config_path=materialized,
        repo_root=repo_root,
        runtime_concurrency=int(os.environ.get("VIKNOW_HYPERRAG_RUNTIME_CONCURRENCY", "64")),
        sync_bridge_concurrency=int(os.environ.get("VIKNOW_HYPERRAG_SYNC_BRIDGE_CONCURRENCY", "64")),
        default_query_mode=str(os.environ.get("VIKNOW_HYPERRAG_QUERY_MODE", default_mode)),
        default_top_k=int(os.environ.get("VIKNOW_HYPERRAG_TOP_K", str(default_top_k))),
    )
    langfuse_enabled = bool(bundle.observe.get("langfuse_enabled"))
    return AppConfig(
        app=AppSettings(state_dir=state_dir),
        knowledge=KnowledgeConfig(default_library_id=default_library, hyperrag=backend),
        scheduler=SchedulerConfig(unified=SchedulerUnifiedConfig(redis_url=bundle.redis_url)),
        observability=ObservabilityConfig(langfuse=LangfuseConfig(enabled=langfuse_enabled)),
    )


def materialize_hyperrag_config(config: AppConfig) -> Path:
    from viknow3.service.hyperrag.materialize import write_materialized_hyperrag

    return write_materialized_hyperrag(config)
