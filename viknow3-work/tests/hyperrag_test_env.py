"""Ensure viknow_ops is configured before HyperRAG initializes."""

from __future__ import annotations

import os
from pathlib import Path


def _ensure_job_log_dir() -> None:
    preferred = Path.home() / "viknow3" / "job-logs"
    preferred.mkdir(parents=True, exist_ok=True)
    current = os.environ.get("JOB_LOG_DIR", "").strip()
    if current:
        path = Path(current)
        try:
            if path.exists() and not path.is_dir():
                raise NotADirectoryError(current)
            path.mkdir(parents=True, exist_ok=True)
            return
        except OSError:
            pass
    os.environ["JOB_LOG_DIR"] = str(preferred)
    os.environ["HYPERRAG_JOB_LOG_DIR"] = str(preferred)


def bootstrap_hyperrag_config() -> None:
    """HyperRAG uses viknow_ops only — no hyperrag.yml."""
    _env = Path.home() / ".viknow3" / "ops.env"
    if _env.is_file() and not os.environ.get("VIKNOW_OPS_DSN"):
        for line in _env.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    if not os.environ.get("VIKNOW_OPS_DSN"):
        raise RuntimeError(
            "Set VIKNOW_OPS_DSN (and VIKNOW_OPS_SECRET) — HyperRAG is loaded from the ops database, not YAML."
        )
    _ensure_job_log_dir()


def init_hyperrag_for_tests() -> None:
    """Run the same startup hook as the FastAPI lifespan (for in-process tests)."""
    import asyncio

    from viknow3.ops.config.store import configured

    if not configured():
        return
    from viknow3.application.hyperrag.runtime import init_hyperrag

    asyncio.run(init_hyperrag())
