"""Resolve chat LLM endpoint from config/app.yaml + env (same as 5175 private Qwen)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from viknow3.ops.config.runtime_env import load_agent_runtime_env

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")
_SERVER_DEFAULT = "server-default"


@dataclass(frozen=True)
class ResolvedChatModel:
    model: str
    base_url: str
    api_key: str
    temperature: float = 0.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return _ENV_PATTERN.sub(repl, value)


def _load_app_config() -> dict[str, Any]:
    candidates: list[Path] = []
    configured = os.environ.get("VIKNOW_APP_CONFIG", "").strip()
    if configured:
        candidates.append(Path(configured))
    root = _repo_root()
    candidates.extend(
        (
            root / "config" / "app.yaml",
            root / "config" / "local" / "app.yaml",
        )
    )
    for path in candidates:
        if path.is_file():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _model_providers(app_cfg: dict[str, Any]) -> dict[str, Any]:
    share = app_cfg.get("share") if isinstance(app_cfg.get("share"), dict) else {}
    providers = share.get("model_providers")
    if isinstance(providers, dict):
        return providers
    legacy = app_cfg.get("model_providers")
    return legacy if isinstance(legacy, dict) else {}


def _default_model_ref(app_cfg: dict[str, Any]) -> str:
    share = app_cfg.get("share") if isinstance(app_cfg.get("share"), dict) else {}
    share_agent = share.get("agent") if isinstance(share.get("agent"), dict) else {}
    ref = str(share_agent.get("model_ref") or "").strip()
    if ref:
        return ref
    agent_cfg = app_cfg.get("agent") if isinstance(app_cfg.get("agent"), dict) else {}
    model_cfg = agent_cfg.get("model") if isinstance(agent_cfg.get("model"), dict) else {}
    ref = str(model_cfg.get("ref") or "").strip()
    if ref:
        return ref
    env_model = os.environ.get("VIKNOW_PRIVATE_MODEL", "").strip()
    if env_model:
        return f"private:{env_model}"
    return ""


def _agent_temperature(app_cfg: dict[str, Any]) -> float:
    agent_cfg = app_cfg.get("agent") if isinstance(app_cfg.get("agent"), dict) else {}
    model_cfg = agent_cfg.get("model") if isinstance(agent_cfg.get("model"), dict) else {}
    raw = model_cfg.get("temperature")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _provider_credentials(provider_name: str, providers: dict[str, Any]) -> tuple[str, str] | None:
    block = providers.get(provider_name)
    if not isinstance(block, dict):
        return None
    base_env = str(block.get("base_url_env") or "").strip()
    key_env = str(block.get("api_key_env") or "").strip()
    if not base_env or not key_env:
        return None
    base_url = os.environ.get(base_env, "").strip()
    api_key = os.environ.get(key_env, "").strip()
    if not base_url or not api_key:
        return None
    return base_url, api_key


def _match_model_option(app_cfg: dict[str, Any], selector: str) -> str | None:
    agent_cfg = app_cfg.get("agent") if isinstance(app_cfg.get("agent"), dict) else {}
    options = agent_cfg.get("model_options") or []
    if not isinstance(options, list):
        return None
    normalized = selector.strip().lower()
    for option in options:
        if not isinstance(option, dict):
            continue
        ref = str(option.get("ref") or "")
        if not ref:
            continue
        label = str(option.get("label") or "")
        model_name = ref.split(":", 1)[1] if ":" in ref else ref
        candidates = {ref.lower(), label.lower(), model_name.lower()}
        if normalized in candidates:
            return ref
    return None


def _parse_ref(ref: str) -> tuple[str, str]:
    expanded = _expand_env(ref.strip())
    if ":" in expanded:
        provider, model = expanded.split(":", 1)
        return provider.strip(), model.strip()
    return "private", expanded


def resolve_openai_chat(model_selector: str | None = None) -> ResolvedChatModel:
    """Resolve OpenAI-compatible chat endpoint for LangChain ChatOpenAI."""
    load_agent_runtime_env()
    app_cfg = _load_app_config()
    providers = _model_providers(app_cfg)
    temperature = _agent_temperature(app_cfg)

    selector = (model_selector or "").strip()
    if not selector or selector == _SERVER_DEFAULT:
        ref = _default_model_ref(app_cfg)
        if not ref:
            raise RuntimeError(
                "No default chat model: set share.agent.model_ref in config/app.yaml "
                "or VIKNOW_PRIVATE_MODEL in ops env"
            )
        provider_name, model_name = _parse_ref(ref)
    elif selector.startswith("private:") or selector.startswith("siliconflow:"):
        provider_name, model_name = _parse_ref(selector)
    else:
        matched = _match_model_option(app_cfg, selector)
        if matched:
            provider_name, model_name = _parse_ref(matched)
        elif selector.startswith("siliconflow:") or selector.startswith("private:"):
            provider_name, model_name = _parse_ref(selector)
        else:
            creds = _provider_credentials("private", providers)
            if creds and os.environ.get("VIKNOW_PRIVATE_MODEL"):
                provider_name, model_name = "private", selector
            else:
                provider_name, model_name = _parse_ref(f"siliconflow:{selector}")

    creds = _provider_credentials(provider_name, providers)
    if creds is None:
        raise RuntimeError(
            f"Chat provider {provider_name!r} is not configured: "
            f"check config/app.yaml model_providers and env vars"
        )
    base_url, api_key = creds
    if not model_name:
        raise RuntimeError(f"Empty model name for provider {provider_name!r}")
    return ResolvedChatModel(model=model_name, base_url=base_url, api_key=api_key, temperature=temperature)
