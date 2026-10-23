"""Point unittest at *_test Postgres databases (see deploy/viknow3-dev-data/init-databases.sh)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _docker_pg_password() -> str | None:
    try:
        out = subprocess.check_output(
            ["docker", "exec", "viknow3-ops-postgres", "printenv", "POSTGRES_PASSWORD"],
            text=True,
            timeout=8,
        ).strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def _postgres_base_from_files() -> str | None:
    repo = Path(__file__).resolve().parents[1]
    candidates = [
        Path.home() / ".viknow3" / "ops.env",
        repo / "deploy" / "demo" / "pg-demo.env",
        repo / "deploy" / "demo" / "runtime.env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        values = _parse_env_file(path)
        for key in ("VIKNOW_HYPERRAG_DSN", "VIKNOW_ACCESS_DSN", "VIKNOW_AGENT_DSN"):
            dsn = values.get(key, "").strip()
            if dsn.startswith("postgresql://") and "/" in dsn:
                return dsn.rsplit("/", 1)[0]
    return None


def apply() -> None:
    """Call before importing viknow3 modules that open DB connections."""
    base = os.environ.get("VIKNOW_TEST_PG_BASE", "").strip().rstrip("/")
    if not base:
        base = _postgres_base_from_files() or ""
    if not base:
        user = os.environ.get("VIKNOW_TEST_PG_USER", "viknow_ops")
        password = os.environ.get("VIKNOW_TEST_PG_PASSWORD", "") or _docker_pg_password() or ""
        if not password:
            raise RuntimeError(
                "Set VIKNOW_TEST_PG_PASSWORD or provide ~/.viknow3/ops.env / deploy/demo/pg-demo.env for unittest"
            )
        host = os.environ.get("VIKNOW_TEST_PG_HOST", "127.0.0.1")
        port = os.environ.get("VIKNOW_TEST_PG_PORT", "16432")
        base = f"postgresql://{user}:{password}@{host}:{port}"

    os.environ["VIKNOW_ACCESS_DSN"] = f"{base}/viknow_access_test"
    os.environ["VIKNOW_AGENT_DSN"] = f"{base}/viknow_agent_test"
    os.environ["VIKNOW_HYPERRAG_DSN"] = f"{base}/viknow3_knowledge_test"
