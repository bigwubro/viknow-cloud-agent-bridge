"""Static configuration requirements for registered ViKnow tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from agents.expert_buddy.core.config import (
    AgentModelConfig,
    AgentModelOption,
    AppConfig,
    ModelProviderConfig,
    ToolConfigs,
    ToolModelConfig,
)
from agents.expert_buddy.core.runtime.domain import AgentProfile


@dataclass(frozen=True)
class ToolConfigRequirement:
    """Typed config and credentials required by one Tool ID."""

    group: Literal["knowledge", "multimodal", "monitoring"]
    service: str | None = None
    credential_fields: tuple[str, ...] = ()


TOOL_CONFIG_REQUIREMENTS: Mapping[str, ToolConfigRequirement] = MappingProxyType(
    {
        "knowledge_discover": ToolConfigRequirement("knowledge"),
        "knowledge_search": ToolConfigRequirement("knowledge"),
        "list_video_segments": ToolConfigRequirement("knowledge"),
        "video_inspect": ToolConfigRequirement(
            "multimodal",
            "review_model",
            ("api_key_env",),
        ),
        "video_inspect_2_0": ToolConfigRequirement("multimodal", "review_model", ("api_key_env",)),
        "image_inspect": ToolConfigRequirement(
            "multimodal",
            "review_model",
            ("api_key_env",),
        ),
        "document_inspect": ToolConfigRequirement(
            "multimodal",
            "review_model",
            ("api_key_env",),
        ),
        "visual_ground": ToolConfigRequirement(
            "multimodal",
            "locate_anything",
            ("api_key_env",),
        ),
        "text_inspect": ToolConfigRequirement(
            "multimodal",
            "review_model",
            ("api_key_env",),
        ),
        "evidence_asr": ToolConfigRequirement(
            "multimodal",
            "asr",
            ("api_key_env",),
        ),
        "evidence_ocr": ToolConfigRequirement(
            "multimodal",
            "ocr",
            ("api_key_env",),
        ),
        "clip_search": ToolConfigRequirement(
            "multimodal",
            "video_discovery",
            ("api_key_env",),
        ),
        "global_browse": ToolConfigRequirement(
            "multimodal",
            "video_discovery",
            ("api_key_env",),
        ),
        "frame_inspect": ToolConfigRequirement(
            "multimodal",
            "video_discovery",
            ("api_key_env",),
        ),
        "prometheus_query": ToolConfigRequirement("monitoring", "prometheus"),
        "prometheus_query_range": ToolConfigRequirement("monitoring", "prometheus"),
        "prometheus_metric_search": ToolConfigRequirement("monitoring", "prometheus"),
        "vllm_health": ToolConfigRequirement("monitoring", "vllm"),
        "npm_health": ToolConfigRequirement("monitoring", "npm"),
        "npm_proxy_hosts": ToolConfigRequirement(
            "monitoring",
            "npm",
            ("identity_env", "secret_env"),
        ),
        "npm_certificates": ToolConfigRequirement(
            "monitoring",
            "npm",
            ("identity_env", "secret_env"),
        ),
        "litellm_status": ToolConfigRequirement(
            "monitoring",
            "litellm",
            ("api_key_env",),
        ),
        "litellm_models": ToolConfigRequirement(
            "monitoring",
            "litellm",
            ("api_key_env",),
        ),
    }
)
_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def validate_agent_model_credentials(
    config: AppConfig,
    *,
    environ: Mapping[str, str],
) -> None:
    """Validate the default and every selectable Agent model without API calls."""

    _validate_agent_model(
        config.agent.model,
        config.model_providers,
        subject="agent.model",
        environ=environ,
    )
    for index, option in enumerate(config.agent.model_options):
        subject = f"agent.model_options[{index}]"
        model_config = _agent_model_config_for_option(
            config.agent.model,
            option,
            config.model_providers,
            subject=subject,
        )
        _validate_agent_model(
            model_config,
            config.model_providers,
            subject=subject,
            environ=environ,
        )


def validate_tool_requirements(
    config: AppConfig,
    profile: AgentProfile,
    tool_configs: ToolConfigs,
    *,
    environ: Mapping[str, str],
) -> None:
    """Validate enabled Tool config and credentials without external probes."""

    validated_services: set[tuple[str, str | None]] = set()
    for tool_id in profile.enabled_tools:
        requirement = TOOL_CONFIG_REQUIREMENTS.get(tool_id)
        if requirement is None:
            continue
        if requirement.group == "knowledge" and config.knowledge is None:
            raise RuntimeError(f"profile {profile.id} tool {tool_id} requires AppConfig.knowledge")

        group_config = getattr(tool_configs, requirement.group)
        if group_config is None:
            label = (
                "knowledge tool config"
                if requirement.group == "knowledge"
                else f"{requirement.group} tool config"
            )
            raise RuntimeError(f"profile {profile.id} tool {tool_id} requires {label}")

        service_config = group_config
        if requirement.service is not None:
            service_config = getattr(group_config, requirement.service)
            if service_config is None:
                raise RuntimeError(
                    f"profile {profile.id} tool {tool_id} requires "
                    f"{requirement.group}.{requirement.service} config"
                )

        service_key = (requirement.group, requirement.service)
        if service_key not in validated_services and isinstance(service_config, ToolModelConfig):
            _validate_tool_model(
                service_config,
                subject=f"profile {profile.id} tool {tool_id}",
                environ=environ,
            )
        validated_services.add(service_key)
        for credential_field in requirement.credential_fields:
            env_name = getattr(service_config, credential_field)
            _require_environment_value(
                env_name,
                subject=f"profile {profile.id} tool {tool_id}",
                environ=environ,
            )


def _agent_model_config_for_option(
    default: AgentModelConfig,
    option: AgentModelOption,
    model_providers: Mapping[str, ModelProviderConfig],
    *,
    subject: str,
) -> AgentModelConfig:
    if option.ref:
        return default.model_copy(
            update={
                "ref": option.ref,
                "provider_name": None,
                "model": None,
                "model_env": None,
            }
        )
    option_model = option.model
    if option_model is None and option.model_env is None:
        option_model = option.id
    provider_name = option.provider_name or default.provider_name
    if provider_name is None and default.ref:
        provider_name, _model = _split_model_ref(default.ref, subject="agent.model")
    provider_config = (
        _require_model_provider(model_providers, provider_name, subject=subject)
        if provider_name is not None
        else None
    )
    provider = provider_config.provider if provider_config else default.provider
    base_url = provider_config.base_url if provider_config else default.base_url
    base_url_env = provider_config.base_url_env if provider_config else default.base_url_env
    api_key_env = provider_config.api_key_env if provider_config else default.api_key_env
    if option.base_url_env is not None:
        base_url = None
        base_url_env = option.base_url_env
    if option.api_key_env is not None:
        api_key_env = option.api_key_env
    return default.model_copy(
        update={
            "ref": None,
            "provider": provider,
            "provider_name": None,
            "model": option_model,
            "model_env": option.model_env,
            "base_url": base_url,
            "base_url_env": base_url_env,
            "api_key_env": api_key_env,
        }
    )


def _validate_agent_model(
    model_config: AgentModelConfig,
    model_providers: Mapping[str, ModelProviderConfig],
    *,
    subject: str,
    environ: Mapping[str, str],
) -> None:
    provider_config = None
    provider_model = None
    using_gateway_fallback = False
    model = model_config.model
    model_env = model_config.model_env
    if model_config.ref:
        provider_name, model = _split_model_ref(model_config.ref, subject=subject)
        provider_config = _require_model_provider(
            model_providers,
            provider_name,
            subject=subject,
        )
        provider_model = model
        model_env = None
    elif model_config.provider_name:
        provider_config = _require_model_provider(
            model_providers,
            model_config.provider_name,
            subject=subject,
        )

    resolved_model = _require_config_value(
        model,
        model_env,
        subject=subject,
        field_label="model or model_env",
        environ=environ,
    )
    if provider_config is not None:
        fallback_ref = provider_config.fallback_model_refs.get(provider_model or resolved_model)
        if fallback_ref is not None:
            fallback_name, fallback_model = _split_model_ref(fallback_ref, subject=subject)
            fallback_provider = _require_model_provider(
                model_providers,
                fallback_name,
                subject=subject,
            )
            _require_config_value(
                fallback_model,
                None,
                subject=subject,
                field_label="fallback model",
                environ=environ,
            )
            if fallback_model in fallback_provider.fallback_model_refs:
                raise RuntimeError(f"{subject} gateway fallback must reference a direct model")
            provider_config = fallback_provider
            using_gateway_fallback = True
    base_url = provider_config.base_url if provider_config else model_config.base_url
    base_url_env = provider_config.base_url_env if provider_config else model_config.base_url_env
    if using_gateway_fallback:
        _require_config_value(
            base_url,
            base_url_env,
            subject=subject,
            field_label="base_url or base_url_env",
            environ=environ,
        )
    elif _nonempty(base_url):
        _expand_environment_tokens(base_url, subject=subject, environ=environ)
    elif base_url_env:
        _require_environment_value(
            base_url_env,
            subject=subject,
            environ=environ,
        )
    api_key_env = provider_config.api_key_env if provider_config else model_config.api_key_env
    _require_environment_value(
        api_key_env,
        subject=subject,
        environ=environ,
    )


def _validate_tool_model(
    model_config: ToolModelConfig,
    *,
    subject: str,
    environ: Mapping[str, str],
) -> None:
    _require_config_value(
        model_config.model,
        model_config.model_env,
        subject=subject,
        field_label="model or model_env",
        environ=environ,
    )
    _require_config_value(
        model_config.base_url,
        model_config.base_url_env,
        subject=subject,
        field_label="base_url or base_url_env",
        environ=environ,
    )


def _require_config_value(
    explicit: str | None,
    env_name: str | None,
    *,
    subject: str,
    field_label: str,
    environ: Mapping[str, str],
) -> str:
    if _nonempty(explicit):
        return _expand_environment_tokens(explicit, subject=subject, environ=environ)
    if env_name and _nonempty(environ.get(env_name)):
        return environ[env_name].strip()
    if env_name:
        raise RuntimeError(f"{subject} requires {field_label}: {env_name} is unset")
    raise RuntimeError(f"{subject} requires {field_label}")


def _require_environment_value(
    env_name: str | None,
    *,
    subject: str,
    environ: Mapping[str, str],
) -> str:
    if env_name and _nonempty(environ.get(env_name)):
        return environ[env_name].strip()
    if env_name:
        raise RuntimeError(f"{subject} requires {env_name}")
    raise RuntimeError(f"{subject} requires a credential environment variable")


def _nonempty(value: str | None) -> bool:
    return bool(value and value.strip())


def _split_model_ref(ref: str, *, subject: str) -> tuple[str, str]:
    provider_name, separator, model = ref.partition(":")
    provider_name = provider_name.strip()
    model = model.strip()
    if not separator or not provider_name or not model:
        raise RuntimeError(f"{subject} model ref must use provider:model")
    return provider_name, model


def _require_model_provider(
    model_providers: Mapping[str, ModelProviderConfig],
    provider_name: str,
    *,
    subject: str,
) -> ModelProviderConfig:
    provider_config = model_providers.get(provider_name)
    if provider_config is None:
        raise RuntimeError(f"{subject} references unknown model provider: {provider_name}")
    return provider_config


def _expand_environment_tokens(
    value: str,
    *,
    subject: str,
    environ: Mapping[str, str],
) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = environ.get(env_name)
        if not _nonempty(env_value):
            missing.append(env_name)
            return ""
        return env_value.strip()

    expanded = _ENV_TOKEN_RE.sub(replace, value).strip()
    if missing:
        raise RuntimeError(f"{subject} requires {', '.join(missing)}")
    if not expanded:
        raise RuntimeError(f"{subject} resolved to an empty value")
    return expanded
