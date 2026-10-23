"""Reload AppConfig from mounted files without restarting the process.

Storage foundation (Postgres / Redis / files_root / JuiceFS / listen / license)
cannot be hot-swapped. Those changes must rebuild or restart the container.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from viknow.core.config import (
    AppConfig,
    load_app_config,
    load_tool_configs,
    materialize_runtime_tool_configs,
)
from viknow.core.env import apply_deploy_env, restore_env_values
from viknow.core.logging import configure_logging
from viknow.core.profiles import validate_profile_catalog

if TYPE_CHECKING:
    from fastapi import FastAPI


class ConfigReloadError(RuntimeError):
    """Reload was rejected or failed; the previous AppConfig remains in use."""


class ConfigFoundationChanged(ConfigReloadError):
    """A storage/listen/license field changed; SIGHUP cannot apply it."""


@dataclass(frozen=True)
class ConfigFoundation:
    persistence_backend: str
    postgres_host: str | None
    postgres_port: int | None
    postgres_database: str | None
    postgres_user: str | None
    postgres_password: str | None
    files_root: str | None
    redis_url: str
    api_host: str
    api_port: int
    license_enabled: bool
    license_path: str
    juicefs_enabled: str
    trace_console_pool_size: int
    trace_console_query_timeout: float


def config_reload_enabled() -> bool:
    raw = os.getenv("VIKNOW_CONFIG_RELOAD", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def config_foundation(config: AppConfig) -> ConfigFoundation:
    """Identity of fields that require a process or container restart."""

    postgres = config.persistence.postgres
    files_root = config.app.files_root
    return ConfigFoundation(
        persistence_backend=str(config.persistence.backend),
        postgres_host=None if postgres is None else postgres.host,
        postgres_port=None if postgres is None else postgres.port,
        postgres_database=None if postgres is None else postgres.database,
        postgres_user=None if postgres is None else postgres.user,
        postgres_password=None if postgres is None else postgres.password,
        files_root=None if files_root is None else str(files_root),
        redis_url=config.scheduler.unified.redis_url,
        api_host=config.api.host,
        api_port=config.api.port,
        license_enabled=bool(config.license.enabled),
        license_path=str(config.license.license_path),
        trace_console_pool_size=config.trace_console.pool_size,
        trace_console_query_timeout=config.trace_console.query_timeout_seconds,
        juicefs_enabled=os.getenv("VIKNOW_JUICEFS_ENABLED", ""),
    )


def clear_app_config_cache() -> None:
    from viknow.api.app import get_app_config

    get_app_config.cache_clear()

    from viknow.agent_adapters.tool_call_markup import (
        resolve_tool_call_recovery_trace_path,
    )

    resolve_tool_call_recovery_trace_path.cache_clear()


def reload_app_configuration(*, config_path: Path | None = None) -> AppConfig:
    """Re-read app.yaml after deploy.env has been applied by the caller."""

    clear_app_config_cache()
    return load_app_config(config_path)


async def apply_config_reload(app: FastAPI) -> AppConfig:
    """Reload safe runtime fields; keep the previous config on failure."""

    previous: AppConfig = app.state.config
    config_path = previous._config_path
    applied: dict[str, str | None] = {}
    try:
        if config_path is not None:
            applied = apply_deploy_env(config_path)
        new_config = reload_app_configuration(config_path=config_path)
        if config_foundation(new_config) != config_foundation(previous):
            raise ConfigFoundationChanged(
                "storage or listen foundation changed; rebuild or restart the container"
            )
        tool_configs = load_tool_configs(new_config)
        validate_profile_catalog(new_config, new_config.profile_catalog, tool_configs)
        configure_logging(new_config)
    except Exception as exc:
        restore_env_values(applied)
        clear_app_config_cache()
        if isinstance(exc, ConfigReloadError):
            raise
        raise ConfigReloadError(str(exc)) from exc

    app.state.config = new_config
    app.state.profile_catalog = new_config.profile_catalog
    app.state.admin_config = new_config.admin
    app.state.request_timeout_seconds = new_config.api.request_timeout_seconds
    app.state.tool_configs = tool_configs
    if getattr(app.state, "runtime_tool_configs_ready", False):
        app.state.tool_configs = materialize_runtime_tool_configs(new_config, tool_configs)

    registry = getattr(app.state, "agent_runtime_registry", None)
    if registry is not None:
        registry.reset_profile_cache(new_config.profile_catalog)

    adapter = getattr(app.state, "knowledge_adapter", None)
    if adapter is not None and new_config.knowledge is not None:
        adapter.reload_config(new_config)
    elif new_config.knowledge is not None:
        from viknow.core.knowledge.hyperrag_adapter import build_hyperrag_adapter

        app.state.knowledge_adapter = build_hyperrag_adapter(new_config)
    else:
        app.state.knowledge_adapter = None

    logger.info("reloaded ViKnow configuration from {}", config_path)
    return new_config


def register_config_reload_signal(app: FastAPI) -> None:
    """Install the SIGHUP handler used to trigger mounted config reload."""

    if not config_reload_enabled():
        return

    app.state.config_reload_event = asyncio.Event()

    def _on_sighup(_signum: int, _frame: object | None) -> None:
        event: asyncio.Event | None = getattr(app.state, "config_reload_event", None)
        if event is not None:
            event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGHUP, _on_sighup)
        return
    logger.warning(
        "SIGHUP handler not installed outside the main thread; use POST /api/v1/admin/config/reload"
    )


async def start_config_reload_watcher(app: FastAPI) -> None:
    if not config_reload_enabled():
        return
    if getattr(app.state, "config_reload_event", None) is None:
        app.state.config_reload_event = asyncio.Event()

    async def _watch_config_reload() -> None:
        while True:
            await app.state.config_reload_event.wait()
            app.state.config_reload_event.clear()
            try:
                await apply_config_reload(app)
            except ConfigReloadError as exc:
                logger.error("config reload via SIGHUP failed: {}", exc)

    app.state.config_reload_task = asyncio.create_task(_watch_config_reload())


async def stop_config_reload_watcher(app: FastAPI) -> None:
    task = getattr(app.state, "config_reload_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        app.state.config_reload_task = None
