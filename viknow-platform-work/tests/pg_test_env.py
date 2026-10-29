"""Point unittest at *_test Postgres on externally provisioned PG (viknow-platform does not start DB)."""

from __future__ import annotations

import os
from pathlib import Path


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
        Path.home() / ".viknow-platform" / "ops.env",
        repo / "deploy" / "236" / "env" / "pg-demo.env",
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


def apply_if_configured() -> bool:
    """Set *_test DSN env vars when credentials are available; otherwise no-op."""
    if os.environ.get("VIKNOW_ACCESS_DSN") and os.environ.get("VIKNOW_HYPERRAG_DSN"):
        return True
    try:
        apply()
        return True
    except RuntimeError:
        return False


def apply() -> None:
    """Call before importing viknow-platform modules that open DB connections."""
    base = os.environ.get("VIKNOW_TEST_PG_BASE", "").strip().rstrip("/")
    if not base:
        base = _postgres_base_from_files() or ""
    if not base:
        password = os.environ.get("VIKNOW_TEST_PG_PASSWORD", "").strip()
        if not password:
            raise RuntimeError(
                "Set VIKNOW_TEST_PG_PASSWORD or VIKNOW_TEST_PG_BASE, "
                "or provide DSN prefix in ~/.viknow-platform/ops.env or deploy/236/env/pg-demo.env "
                "(Postgres must be provisioned outside viknow-platform; see docs/external-infra.md)"
            )
        user = os.environ.get("VIKNOW_TEST_PG_USER", "viknow_ops")
        host = os.environ.get("VIKNOW_TEST_PG_HOST", "127.0.0.1")
        port = os.environ.get("VIKNOW_TEST_PG_PORT", "16432")
        base = f"postgresql://{user}:{password}@{host}:{port}"

    os.environ["VIKNOW_ACCESS_DSN"] = f"{base}/viknow_access_test"
    os.environ["VIKNOW_AGENT_DSN"] = f"{base}/viknow_agent_test"
    os.environ["VIKNOW_HYPERRAG_DSN"] = f"{base}/viknow_knowledge_test"
