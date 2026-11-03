"""Write HyperRAG runtime yaml from viknow_ops (ephemeral file for the engine only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from viknow_platform.application.hyperrag.config import AppConfig
from viknow_platform.application.hyperrag.ops_bootstrap import build_hyperrag_payload_from_ops
from viknow_platform.ops.config.store import configured


def build_materialized_hyperrag(_config: AppConfig) -> dict[str, Any]:
    if not configured():
        raise RuntimeError("VIKNOW_OPS_DSN is required; HyperRAG is not configured via YAML files")
    return build_hyperrag_payload_from_ops(state_dir=_config.app.state_dir)


def write_materialized_hyperrag(config: AppConfig) -> Path:
    payload = build_materialized_hyperrag(config)
    target_dir = config.app.state_dir / "hyperrag"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "hyperrag.yml"
    with target.open("w", encoding="utf-8") as file:
        yaml.safe_dump(payload, file, sort_keys=False, allow_unicode=True)
    return target
