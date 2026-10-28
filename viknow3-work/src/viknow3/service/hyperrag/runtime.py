"""Process-level HyperRAG engine lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from viknow3.ops.config.store import configured

if TYPE_CHECKING:
    from viknow3.service.hyperrag.adapter import HyperRAGAdapter


log = logging.getLogger(__name__)

_adapter: HyperRAGAdapter | None = None
_hyperrag_started: bool = False

_NOT_INITIALIZED = (
    "HyperRAG is not initialized. The application must call init_hyperrag() "
    "during process startup (before serving requests)."
)


def _prepare_hyperrag_process_env() -> None:
    """Ensure HyperRAG log paths exist before the engine imports setup_logger()."""

    preferred = (
        os.environ.get("HYPERRAG_JOB_LOG_DIR", "").strip(),
        os.environ.get("JOB_LOG_DIR", "").strip(),
        "/tmp/viknow3-job-logs",
    )
    fallback = Path("/tmp/viknow3-job-logs")
    for raw in preferred:
        if not raw:
            continue
        path = Path(raw)
        if path.exists() and not path.is_dir():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
            resolved = str(path.resolve())
            os.environ["JOB_LOG_DIR"] = resolved
            os.environ["HYPERRAG_JOB_LOG_DIR"] = resolved
            return
        except OSError:
            continue
    fallback.mkdir(parents=True, exist_ok=True)
    resolved = str(fallback.resolve())
    os.environ["JOB_LOG_DIR"] = resolved
    os.environ["HYPERRAG_JOB_LOG_DIR"] = resolved


def install_actio_port() -> None:
    """Give the HyperRAG engine the Actio client. The engine does not import actio."""

    from hyperrag.actio_port import install
    from viknow3.service.actio.client import indexing_bindings

    install(**indexing_bindings())


def get_engine_adapter() -> HyperRAGAdapter:
    if _adapter is None:
        raise RuntimeError(_NOT_INITIALIZED)
    return _adapter


def _bootstrap_engine_adapter() -> None:
    global _adapter
    if _adapter is not None:
        return
    from viknow3.service.hyperrag.adapter import build_hyperrag_adapter
    from viknow3.service.hyperrag.config import load_app_config, materialize_hyperrag_config

    config = load_app_config()
    materialize_hyperrag_config(config)
    _adapter = build_hyperrag_adapter(config)


async def init_hyperrag() -> None:
    """Materialize ops config, start the adapter runtime, and bind the HTTP client."""

    global _hyperrag_started
    if _hyperrag_started:
        return
    if not configured():
        return

    _prepare_hyperrag_process_env()
    try:
        install_actio_port()
        from viknow3.service.hyperrag.storage_video_split import install_storage_video_split

        install_storage_video_split()
    except Exception:
        log.warning("Actio client was not installed; video indexing stays unavailable", exc_info=True)
    _bootstrap_engine_adapter()
    from viknow3.service.hyperrag.client import init_hyperrag_client

    init_hyperrag_client()
    await start_background_runtime()
    _hyperrag_started = True


async def start_background_runtime() -> None:
    await get_engine_adapter().start_background_runtime()


async def shutdown_background_runtime() -> None:
    global _adapter, _hyperrag_started
    from viknow3.service.hyperrag.client import reset_hyperrag_client

    adapter = _adapter
    if adapter is None:
        reset_hyperrag_client()
        _hyperrag_started = False
        return
    loop = adapter._runtime_loop  # noqa: SLF001 — coordinated shutdown
    if loop is not None and loop.is_running():
        app = adapter._app  # noqa: SLF001
        if app is not None:
            future = asyncio.run_coroutine_threadsafe(app.shutdown(), loop)
            try:
                future.result(timeout=120)
            except Exception:
                pass
    _adapter = None
    reset_hyperrag_client()
    _hyperrag_started = False
