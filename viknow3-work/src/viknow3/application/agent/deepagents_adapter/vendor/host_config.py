"""Assemble one agent runtime from platform ops data and the shared kernel file.

``viknow3_ops`` supplies model endpoints. ``src/agents/runtimes/deepagents/runtime.yaml`` supplies
the shared loop. The agent directory supplies the profile. Nothing here reads
``app.yaml``.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.runtimes.deepagents.core.config import (
    AgentModelConfig,
    AgentModelOption,
    AgentRuntimeConfig,
    ApiConfig,
    AppConfig,
    AppSettings,
    ModelProviderConfig,
    MultimodalReviewModelConfig,
    MultimodalToolConfig,
    PersistenceBackend,
    ShareAgentConfig,
    ShareConfig,
    ToolConfigs,
    resolve_model_config,
)
from agents.runtimes.deepagents.core.scheduler.unified_config import SchedulerSectionConfig, UnifiedSchedulerAppConfig
from agents.runtimes.deepagents.runtime_config import RUNTIME_CONFIG_PATH, load_kernel_config, load_model_defaults
from viknow3.pg import AGENT, CHECKPOINT_SCHEMA, dsn, ensure_schema, with_search_path
from viknow3.ops.config.model_resolve import (
    PlatformCatalog,
    agent_model_ref,
    load_platform_catalog,
    provider_env_names,
    publish_catalog_env,
)


def build_agent_app_config(
    *,
    skills_dir: Path,
    profile_dir: Path,
    agent_ids: tuple[str, ...],
    files_root: Path,
    catalog: PlatformCatalog | None = None,
    static_dir: Path | None = None,
) -> AppConfig:
    """Build the AppConfig the shared runtime still expects, without a yaml pack."""
    chat = catalog if catalog is not None else load_platform_catalog(*agent_ids)
    publish_catalog_env(chat)
    defaults = load_model_defaults()
    model_ref = agent_model_ref(chat)
    files_root.mkdir(parents=True, exist_ok=True)
    state_dir = files_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    ensure_schema(AGENT, CHECKPOINT_SCHEMA)
    kernel = load_kernel_config()
    kernel = kernel.model_copy(
        update={
            "checkpointer": kernel.checkpointer.model_copy(
                update={
                    "enabled": True,
                    "backend": PersistenceBackend.POSTGRES,
                    "postgres_dsn": with_search_path(dsn(AGENT), CHECKPOINT_SCHEMA),
                }
            )
        }
    )
    providers = {
        provider_id: ModelProviderConfig(
            base_url_env=provider_env_names(provider_id)[0],
            api_key_env=provider_env_names(provider_id)[1],
        )
        for provider_id, provider in chat.providers.items()
        if provider.base_url
    }
    options = [
        AgentModelOption(
            ref=f"{row.provider_id}:{row.name}",
            label=row.label or row.name,
            max_input_tokens=defaults.max_input_tokens,
        )
        for row in chat.models
        if row.provider_id in providers and row.name
    ]
    if model_ref not in {option.selection_id() for option in options}:
        options.append(AgentModelOption(ref=model_ref, label=model_ref, max_input_tokens=defaults.max_input_tokens))

    config = AppConfig(
        app=AppSettings(
            name="viknow",
            state_dir=state_dir,
            files_root=files_root,
        ),
        api=ApiConfig(
            static_dir=static_dir or files_root,
            host=os.environ.get("VIKNOW_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("VIKNOW_API_PORT", "5198")),
        ),
        share=ShareConfig(
            model_providers=providers,
            files_root=files_root,
            agent=ShareAgentConfig(model_ref=model_ref),
            scheduler=SchedulerSectionConfig(unified=UnifiedSchedulerAppConfig(enabled=False)),
        ),
        agent=AgentRuntimeConfig(
            default_profile=agent_ids[0] if agent_ids else "expert_buddy",
            profile_dir=profile_dir,
            skill_dirs=[skills_dir],
            model=AgentModelConfig(
                ref=model_ref,
                temperature=defaults.temperature,
                repetition_penalty=defaults.repetition_penalty,
                max_input_tokens=defaults.max_input_tokens,
                timeout_seconds=defaults.timeout_seconds,
            ),
            model_options=options,
            deepagents=kernel,
        ),
    )
    config._config_path = RUNTIME_CONFIG_PATH
    return config


def review_tool_configs(config: AppConfig) -> ToolConfigs:
    """Point inspect and query_refine at the platform chat model. No tools yaml."""
    resolved = resolve_model_config(config.agent.model, config.model_providers, field_name="agent.model")
    review = MultimodalReviewModelConfig(
        model=resolved.require_model(),
        base_url_env=resolved.base_url_env,
        api_key_env=resolved.api_key_env,
        timeout_seconds=resolved.timeout_seconds or 180,
    )
    return ToolConfigs(multimodal=MultimodalToolConfig(review_model=review))
