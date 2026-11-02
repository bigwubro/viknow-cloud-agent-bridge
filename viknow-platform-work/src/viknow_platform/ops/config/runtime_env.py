"""Merge ViKnow runtime env files (same sources as Playground / 5175 stack)."""

from __future__ import annotations

import os
from pathlib import Path


def _merge_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))




def _normalize_actio_server_url_for_host() -> None:
    raw = os.environ.get("ACTIO_SERVER_URL", "").strip()
    if not raw:
        return
    normalized = raw.replace("host.docker.internal", "127.0.0.1")
    if ":8871" in normalized:
        normalized = normalized.replace(":8871", ":8600")
    if normalized != raw:
        os.environ["ACTIO_SERVER_URL"] = normalized



def _normalize_private_base_url_for_host() -> None:
    raw = os.environ.get("VIKNOW_PRIVATE_BASE_URL", "").strip().rstrip("/")
    if raw.startswith("http://") and ":8443" in raw:
        os.environ["VIKNOW_PRIVATE_BASE_URL"] = "https://" + raw[len("http://") :]
        return
    if not raw:
        os.environ.setdefault("VIKNOW_PRIVATE_BASE_URL", "https://127.0.0.1:8443/v1")

def load_agent_runtime_env() -> None:
    """Load ops env without overwriting variables already set in the process."""
    primary = os.environ.get("VIKNOW_ENV_FILE", "").strip()
    if primary:
        for part in primary.split(":"):
            part = part.strip()
            if part:
                _merge_env_file(Path(part))
    else:
        for path in (
            Path.home() / "viknow-platform" / "pg-demo.env",
            Path.home() / "deploy.env",
            Path.home() / ".viknow-platform" / "ops.env",
        ):
            _merge_env_file(path)
    _merge_env_file(Path.home() / ".viknow-platform" / "ops.env")
    _normalize_actio_server_url_for_host()
    _normalize_private_base_url_for_host()
