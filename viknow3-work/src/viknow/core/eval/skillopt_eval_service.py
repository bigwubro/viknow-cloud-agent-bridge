"""Build an isolated ViKnow configuration for one SkillOpt candidate."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    load_app_config,
    resolve_checkpointer_sqlite_path,
)
from viknow.core.eval.skillopt_experiment import (
    SkillOptExperimentManifest,
    validate_skillopt_experiment_copy,
)
from viknow.core.profiles import load_profile_catalog
from viknow.core.skills import discover_skills


def build_skillopt_experiment_config(
    manifest: SkillOptExperimentManifest,
    *,
    expected_target_skill_sha256: str | None = None,
) -> AppConfig:
    """Build an in-memory config isolated from shared run storage and Redis db0."""

    expected_sha256 = expected_target_skill_sha256 or manifest.experiment_skill_sha256
    validate_skillopt_experiment_copy(
        manifest,
        expected_target_skill_sha256=expected_sha256,
    )
    base = load_app_config(manifest.base_config_path)
    state_dir = manifest.experiment_root / "state"
    checkpointer = base.agent.deepagents.checkpointer.model_copy(
        update={
            "backend": PersistenceBackend.SQLITE,
            "sqlite_path": resolve_checkpointer_sqlite_path(state_dir=state_dir),
            "postgres_dsn": None,
        }
    )
    deepagents = base.agent.deepagents.model_copy(
        update={
            "state_root": state_dir / "deepagents",
            "workspace": base.agent.deepagents.workspace.model_copy(
                update={"root_dir": state_dir / "workspaces"}
            ),
            "checkpointer": checkpointer,
        }
    )
    agent = base.agent.model_copy(
        update={
            "default_profile": manifest.profile_id,
            "profile_dir": manifest.experiment_profile_dir,
            "skill_dirs": [manifest.experiment_skill_root],
            "deepagents": deepagents,
        }
    )
    redis = base.share.redis.model_copy(update={"database": str(manifest.redis_database)})
    unified = base.scheduler.unified.model_copy(
        update={
            "redis_url": _redis_url_with_database(
                base.scheduler.unified.redis_url,
                manifest.redis_database,
            )
        }
    )
    share = base.share.model_copy(
        update={
            "redis": redis,
            "scheduler": base.scheduler.model_copy(update={"unified": unified}),
        }
    )
    knowledge = base.knowledge
    if knowledge is not None:
        knowledge = knowledge.model_copy(
            update={
                "hyperrag": knowledge.hyperrag.model_copy(
                    update={"eager_start": False, "eager_start_env": None}
                ),
                "feishu": knowledge.feishu.model_copy(
                    update={"enabled": False, "enabled_env": None}
                ),
            }
        )
    observability = base.observability.model_copy(
        update={
            "langfuse": base.observability.langfuse.model_copy(
                update={
                    "enabled": False,
                    "enabled_env": None,
                    "index_enabled": False,
                    "usage_push": base.observability.langfuse.usage_push.model_copy(
                        update={"enabled": False, "callback_url": None}
                    ),
                }
            ),
            "otel": base.observability.otel.model_copy(
                update={"enabled": False, "enabled_env": None}
            ),
        }
    )
    config = base.model_copy(
        update={
            "app": base.app.model_copy(update={"state_dir": state_dir}),
            "agent": agent,
            "share": share,
            "knowledge": knowledge,
            "observability": observability,
            "memory": base.memory.model_copy(update={"mem0": None}),
            "persistence": base.persistence.model_copy(
                update={"backend": PersistenceBackend.SQLITE}
            ),
            "learning": base.learning.model_copy(
                update={"enabled": False, "auto_project_completed_runs": False}
            ),
            "self_learning_analysis": base.self_learning_analysis.model_copy(
                update={"enabled": False}
            ),
            "self_learning_oracle": base.self_learning_oracle.model_copy(update={"enabled": False}),
            "self_learning_skill_optimization": (
                base.self_learning_skill_optimization.model_copy(update={"enabled": False})
            ),
        }
    )
    config._config_path = base.config_path
    config._profile_catalog = load_profile_catalog(
        manifest.experiment_profile_dir,
        default_profile_id=manifest.profile_id,
    )
    discovered = discover_skills(config.agent.skill_dirs)
    expected_skills = {manifest.target_skill_name, *manifest.frozen_skill_sha256}
    if set(discovered) != expected_skills:
        raise ValueError("experiment skill root does not match the manifest")
    return config


def _redis_url_with_database(redis_url: str, database: int) -> str:
    parsed = urlsplit(redis_url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.netloc:
        raise ValueError("experiment requires a valid Redis URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))
