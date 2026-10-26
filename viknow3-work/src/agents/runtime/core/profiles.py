"""Environment-scoped Agent Profile loading and selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from agents.runtime.core.runtime.domain import AgentProfile

if TYPE_CHECKING:
    from agents.runtime.core.config import AppConfig, ToolConfigs

type CapabilitySignature = tuple[frozenset[str], frozenset[str]]

LEGACY_PROFILE_CAPABILITY_SIGNATURES: dict[str, tuple[CapabilitySignature, ...]] = {
    "ops": (
        (
            frozenset(
                {
                    "prometheus_metric_search",
                    "prometheus_query",
                    "prometheus_query_range",
                    "vllm_health",
                    "litellm_status",
                    "litellm_models",
                    "npm_health",
                    "npm_proxy_hosts",
                    "npm_certificates",
                }
            ),
            frozenset(),
        ),
    ),
}


class UnknownProfile(LookupError):
    """Raised when a requested Agent Profile is not in the active Catalog."""


class AgentProfileCatalog:
    """Validated Agent Profiles available to one application environment."""

    def __init__(self, profiles: dict[str, AgentProfile], default_profile_id: str) -> None:
        if default_profile_id not in profiles:
            raise ValueError(f"default profile not found: {default_profile_id}")
        self._profiles = dict(profiles)
        self._default_profile_id = default_profile_id

    @property
    def default_profile_id(self) -> str:
        return self._default_profile_id

    def require(self, profile_id: str) -> AgentProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise UnknownProfile(profile_id) from exc

    def resolve(self, profile_id: str | None) -> AgentProfile:
        return self.require(profile_id or self.default_profile_id)

    def all(self) -> tuple[AgentProfile, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))


def profile_capability_signatures(
    catalog: AgentProfileCatalog,
) -> dict[str, tuple[CapabilitySignature, ...]]:
    """Return current and versioned legacy capability signatures by Profile."""

    signatures: dict[str, tuple[CapabilitySignature, ...]] = {}
    for profile in catalog.all():
        current = (
            frozenset(profile.enabled_tools),
            frozenset(profile.enabled_skills),
        )
        accepted = (current, *LEGACY_PROFILE_CAPABILITY_SIGNATURES.get(profile.id, ()))
        signatures[profile.id] = tuple(dict.fromkeys(accepted))
    return signatures


def load_profile_catalog(
    profile_dir: Path,
    *,
    default_profile_id: str,
) -> AgentProfileCatalog:
    """Load direct child YAML Profile files from one environment directory."""

    if not profile_dir.exists():
        raise FileNotFoundError(profile_dir)
    if not profile_dir.is_dir():
        raise ValueError(f"profile_dir must be a directory: {profile_dir}")

    profile_paths = sorted(profile_dir.glob("*.yaml"))
    if not profile_paths:
        raise ValueError(f"no agent profiles found: {profile_dir}")

    profiles: dict[str, AgentProfile] = {}
    for path in profile_paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"profile file must contain a mapping: {path}")
        profile = AgentProfile.model_validate(raw)
        if path.stem != profile.id:
            raise ValueError(
                f"profile filename must match profile id: {path.stem!r} != {profile.id!r}"
            )
        if profile.id in profiles:
            raise ValueError(f"duplicate agent profile id: {profile.id}")
        profiles[profile.id] = profile

    return AgentProfileCatalog(profiles, default_profile_id)


def validate_profile_catalog(
    config: AppConfig,
    catalog: AgentProfileCatalog,
    tool_configs: ToolConfigs,
    *,
    environ: Mapping[str, str] = os.environ,
) -> None:
    """Validate every selectable Profile and dependency without external probes."""

    from agents.runtime.agent_adapters.hermes import HERMES_COMPATIBLE_TOOL_IDS
    from agents.runtime.core.config import AgentKernelType
    from agents.runtime.core.monitoring import MONITORING_TOOL_IDS
    from agents.runtime.core.skills import discover_skills, resolve_enabled_skills
    from agents.runtime.core.tools import build_viknow_tool_specs
    from agents.runtime.core.tools.requirements import (
        validate_agent_model_credentials,
        validate_tool_requirements,
    )

    validate_agent_model_credentials(config, environ=environ)
    discover_skills(config.agent.skill_dirs)
    if config.agent.kernel == AgentKernelType.DEEPAGENTS:
        supported = {spec.id for spec in build_viknow_tool_specs()} | set(MONITORING_TOOL_IDS)
    elif config.agent.kernel == AgentKernelType.HERMES:
        supported = set(HERMES_COMPATIBLE_TOOL_IDS)
    else:
        raise ValueError(f"unsupported agent kernel: {config.agent.kernel}")

    for profile in catalog.all():
        unknown = sorted(set(profile.enabled_tools) - supported)
        if unknown:
            raise RuntimeError(f"profile {profile.id} has unsupported tools: {', '.join(unknown)}")
        try:
            resolve_enabled_skills(
                config.agent.skill_dirs,
                profile.enabled_skills,
                enabled_tools=profile.enabled_tools,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"profile {profile.id}: {exc}") from exc
        validate_tool_requirements(
            config,
            profile,
            tool_configs,
            environ=environ,
        )
        if config.agent.kernel == AgentKernelType.DEEPAGENTS and (
            config.agent.deepagents.multi_agent.enabled or profile.multi_agent_enabled
        ):
            from agents.runtime.core.subagents import load_profile_subagents, validate_profile_subagents

            validate_profile_subagents(
                load_profile_subagents(profile.id, config.agent.profile_dir),
                profile_id=profile.id,
                supported_tools=supported,
                skill_dirs=config.agent.skill_dirs,
                profile_skills=profile.enabled_skills,
            )
