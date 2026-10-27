"""Shared DeepAgents kernel settings.

Platform connections and model endpoints stay in ``viknow_ops``.
One agent's tools, skills, and prompt stay in ``src/agents/<agent>/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

RUNTIME_CONFIG_PATH = Path(__file__).with_name("runtime.yaml")


@dataclass(frozen=True)
class ModelDefaults:
    """Sampling defaults for every agent that uses this runtime."""

    temperature: float = 0.7
    repetition_penalty: float = 1.0
    max_input_tokens: int = 156000
    timeout_seconds: float = 600.0


def load_runtime_document() -> dict[str, Any]:
    raw = yaml.safe_load(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{RUNTIME_CONFIG_PATH} must contain a mapping")
    return raw


def load_model_defaults() -> ModelDefaults:
    raw = load_runtime_document().get("model") or {}
    if not isinstance(raw, dict):
        raise ValueError("runtime.yaml model must be a mapping")
    return ModelDefaults(
        temperature=float(raw.get("temperature", 0.7)),
        repetition_penalty=float(raw.get("repetition_penalty", 1.0)),
        max_input_tokens=int(raw.get("max_input_tokens", 156000)),
        timeout_seconds=float(raw.get("timeout_seconds", 600)),
    )


def load_kernel_config():
    """DeepAgents kernel settings from ``runtime.yaml``. The ``model`` block is not kernel config."""
    from agents.runtime.core.config import DeepAgentsKernelConfig

    raw = dict(load_runtime_document())
    raw.pop("model", None)
    return DeepAgentsKernelConfig.model_validate(raw)
