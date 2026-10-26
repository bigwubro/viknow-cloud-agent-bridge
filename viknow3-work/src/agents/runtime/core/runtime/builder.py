"""Build configured AgentRuntime implementations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.runtime.agent_adapters.deepagents_config import resolve_deepagents_config
from agents.runtime.core.config import (
    AgentKernelType,
    AppConfig,
    MultiAgentConfig,
    ToolConfigs,
    resolve_model_config,
)
from agents.runtime.core.knowledge.client import KnowledgeClient
from agents.runtime.core.memory import UserMemoryService, build_user_memory_service
from agents.runtime.core.observability.langfuse_project_identity import LangfuseProjectIdentity
from agents.runtime.core.runtime.behavior_snapshot import build_run_behavior_snapshot
from agents.runtime.core.runtime.domain import AgentProfile
from agents.runtime.core.runtime.events import AgentEventStore, AgentRuntime
from agents.runtime.core.skills import resolve_enabled_skills

if TYPE_CHECKING:
    from agents.runtime.core.tools import ToolSpec


def build_agent_runtime(
    config: AppConfig,
    *,
    profile: AgentProfile,
    tool_configs: ToolConfigs,
    workspace_root: Path | None = None,
    event_store: AgentEventStore | None = None,
    knowledge_adapter: KnowledgeClient | None = None,
    user_memory_service: UserMemoryService | None = None,
    extra_tool_specs: tuple[ToolSpec, ...] = (),
    filesystem_manager: object | None = None,
    scheduler_pool_overrides: dict[str, int] | None = None,
    multi_agent_enabled: bool | None = None,
    langfuse_project_identity: LangfuseProjectIdentity | None = None,
    model_factory: Callable[[Any], Any] | None = None,
) -> AgentRuntime:
    """Build a runtime for the explicitly selected profile and prepared tools."""

    root = (workspace_root or Path.cwd()).resolve()
    if config.agent.kernel == AgentKernelType.DEEPAGENTS:
        from agents.runtime.agent_adapters.deepagents import DeepAgentsRuntime
        from agents.runtime.agent_adapters.deepagents_tools import (
            DEEPAGENTS_NATIVE_TOOL_IDS,
            build_deepagents_tools,
        )
        from agents.runtime.agent_adapters.langfuse import build_langfuse_callbacks
        from agents.runtime.core.monitoring import build_monitoring_tool_specs
        from agents.runtime.core.scheduler.config import resource_scheduler_config_with_overrides
        from agents.runtime.core.scheduler.resource_scheduler import ResourceScheduler
        from agents.runtime.core.tools import ViKnowToolHandlers, build_viknow_tool_specs

        deepagents_config = resolve_deepagents_config(config)
        if multi_agent_enabled is not None:
            deepagents_config = deepagents_config.model_copy(
                update={"multi_agent": MultiAgentConfig(enabled=multi_agent_enabled)}
            )
        elif profile.multi_agent_enabled:
            deepagents_config = deepagents_config.model_copy(
                update={"multi_agent": MultiAgentConfig(enabled=True)}
            )
        tool_handlers = ViKnowToolHandlers(
            repo_root=root,
            knowledge_adapter=knowledge_adapter,
            tool_configs=tool_configs,
            filesystem_manager=filesystem_manager,
        )
        monitoring_tool_specs = build_monitoring_tool_specs(
            tool_configs.monitoring,
            enabled_tools=profile.enabled_tools,
        )
        tool_specs = (
            *build_viknow_tool_specs(tool_handlers),
            *monitoring_tool_specs,
            *extra_tool_specs,
        )
        _validate_enabled_tool_ids(
            profile.enabled_tools,
            tool_specs,
            native_tool_ids=DEEPAGENTS_NATIVE_TOOL_IDS,
        )
        behavior_tool_specs = [
            tool_spec for tool_spec in tool_specs if tool_spec.id in set(profile.enabled_tools)
        ]
        skills = resolve_enabled_skills(
            config.agent.skill_dirs,
            profile.enabled_skills,
            enabled_tools=profile.enabled_tools,
        )
        # One scheduler per Agent Profile runtime — not process-wide.
        # Concurrent runs of this profile share it; other profiles do not.
        # Monitoring-only profiles (ops) keep the legacy in-memory scheduler.
        unified_bridge = (
            _build_unified_bridge(config) if _profile_uses_unified_scheduler(profile) else None
        )
        resource_scheduler = ResourceScheduler(
            resource_scheduler_config_with_overrides(scheduler_pool_overrides),
            name=f"profile:{profile.id}",
            unified_bridge=unified_bridge,
        )
        tools = build_deepagents_tools(
            profile,
            tool_handlers=tool_handlers,
            tool_specs=tool_specs,
            scheduler=resource_scheduler,
        )
        subagent_specs = ()
        subagent_skills: list[Any] = []
        if deepagents_config.multi_agent.enabled:
            from agents.runtime.core.subagents import (
                load_profile_subagents,
                resolve_orchestrated_main_skills,
            )

            subagent_specs = load_profile_subagents(profile.id, config.agent.profile_dir)
            if subagent_specs:
                skills = resolve_orchestrated_main_skills(
                    config.agent.skill_dirs,
                    profile.enabled_skills,
                    subagent_specs,
                    enabled_tools=tuple(
                        tool_id
                        for tool_id in profile.enabled_tools
                        if tool_id
                        not in {
                            owned_tool
                            for spec in subagent_specs
                            for owned_tool in spec.enabled_tools
                        }
                    ),
                )
            seen_skill_names: set[str] = {skill.name for skill in skills}
            for spec in subagent_specs:
                for skill in resolve_enabled_skills(
                    config.agent.skill_dirs,
                    spec.enabled_skills,
                    enabled_tools=spec.enabled_tools,
                ):
                    if skill.name in seen_skill_names:
                        continue
                    seen_skill_names.add(skill.name)
                    subagent_skills.append(skill)
        memory_service = user_memory_service or build_user_memory_service(config)
        behavior_snapshot_model = resolve_model_config(
            config.agent.model,
            config.model_providers,
            field_name="agent.model",
        )
        langfuse_callbacks = build_langfuse_callbacks(config.observability.langfuse)

        def current_binding():
            if langfuse_project_identity is None:
                return None
            return langfuse_project_identity.run_binding(
                service_id=config.observability.otel.resolve_service_name(),
                environment=config.config_path.parent.name,
            )

        binding_provider = current_binding if langfuse_project_identity is not None else None
        return DeepAgentsRuntime(
            workspace_root=root,
            profile=profile,
            model_config=config.agent.model,
            config=deepagents_config,
            tools=tools,
            skills=skills,
            subagent_specs=subagent_specs,
            subagent_skills=subagent_skills,
            event_store=event_store,
            langchain_callbacks=langfuse_callbacks,
            langfuse_binding_provider=binding_provider,
            answer_review_model_config=(
                tool_configs.multimodal.review_model if tool_configs.multimodal else None
            ),
            model_options=config.agent.model_options,
            model_providers=config.model_providers,
            user_memory_service=memory_service,
            scheduler=resource_scheduler,
            filesystem_manager=filesystem_manager,
            precompiled_knowledge_loader=tool_handlers.load_precompiled_knowledge_context,
            usage_push_config=config.observability.langfuse.usage_push,
            model_factory=model_factory,
            behavior_snapshot_factory=lambda op: build_run_behavior_snapshot(
                profile=profile,
                agent_model=behavior_snapshot_model,
                skills=skills,
                tool_specs=behavior_tool_specs,
                op=op,
                repo_root=root,
            ),
        )
    if config.agent.kernel == AgentKernelType.HERMES:
        from agents.runtime.agent_adapters.hermes import HermesAgentRuntime

        return HermesAgentRuntime(
            workspace_root=root,
            hermes_home=config.agent.hermes.home,
            profile=profile,
            model_config=config.agent.model,
            model_providers=config.model_providers,
            event_store=event_store,
        )
    raise ValueError(f"unsupported agent kernel: {config.agent.kernel}")


def _profile_uses_unified_scheduler(profile: AgentProfile) -> bool:
    """Profiles that only expose read-only monitoring tools skip Redis unified scheduling."""

    from agents.runtime.core.monitoring import MONITORING_TOOL_IDS

    enabled = set(profile.enabled_tools)
    if not enabled:
        return True
    return not enabled.issubset(MONITORING_TOOL_IDS)


def _build_unified_bridge(config: AppConfig) -> Any:
    """Build a UnifiedSchedulerBridge when the unified scheduler switch is on."""
    unified = config.scheduler.unified
    if not unified.enabled:
        return None
    from agents.runtime.core.scheduler.unified_bridge import UnifiedSchedulerBridge

    scheduler_config = unified.to_scheduler_config()
    return UnifiedSchedulerBridge(scheduler_config)


def resolve_session_workspace_dir(
    config: AppConfig,
    *,
    user_id: str,
    workspace_id: str | None,
    session_id: str,
) -> Path | None:
    """Return the host workspace directory for runtimes that expose one."""

    if config.agent.kernel == AgentKernelType.DEEPAGENTS:
        from agents.runtime.agent_adapters.deepagents import deepagents_session_workspace_dir

        return deepagents_session_workspace_dir(
            resolve_deepagents_config(config),
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    return None


def _validate_enabled_tool_ids(
    enabled_tools: list[str],
    tool_specs,
    *,
    native_tool_ids: frozenset[str] = frozenset(),
) -> None:
    enabled = set(enabled_tools)
    supported = {spec.id for spec in tool_specs}
    unknown = sorted(enabled - supported - native_tool_ids)
    if unknown:
        raise RuntimeError(f"unsupported DeepAgents tool(s): {', '.join(unknown)}")
