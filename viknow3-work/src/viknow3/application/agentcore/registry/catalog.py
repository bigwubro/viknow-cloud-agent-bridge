"""Agent Registry. Profiles name a runtime and the tools that runtime may call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pathlib import Path

import yaml

# Single QA profile exposed to access layer. The agent itself is expert_buddy.
from viknow3.clock import now, stamp

_PROFILE_PATH = (
    Path(__file__).resolve().parents[4] / "agents" / "expert_buddy" / "profile" / "expert_buddy.yaml"
)


def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text(encoding="utf-8"))


_V2_PROFILE = _load_profile()

ALLOWED_MODELS = ("siliconflow:Qwen/Qwen3.6-35B-A3B",)
SERVER_DEFAULT_MODEL = "server-default"

AGENT_DEEPAGENTS = "deepagents"


@dataclass
class Profile:
    id: str
    label: str
    description: str
    default_model: str | None
    runtime: str
    tools: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "default_model": self.default_model,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
        }


def _profile(
    agent_id: str,
    label: str,
    description: str,
    created: datetime,
    runtime: str = "langgraph",
    tools: tuple[str, ...] = (),
) -> Profile:
    return Profile(agent_id, label, description, None, runtime, tools, created, created)


def _server_default_label() -> str:
    from viknow3.ops.config.model_resolve import resolve_openai_chat

    try:
        return f"服务端默认（{resolve_openai_chat(SERVER_DEFAULT_MODEL).model}）"
    except Exception:
        return "服务端默认"


class Registry:
    def __init__(self) -> None:
        created = now()
        shared_tools = tuple(str(item) for item in _V2_PROFILE.get("enabled_tools") or ())
        self._profiles = {
            AGENT_DEEPAGENTS: _profile(
                AGENT_DEEPAGENTS,
                str(_V2_PROFILE["label"]),
                f"{_V2_PROFILE['description']}（expert_buddy）",
                created,
                "langgraph",
                shared_tools,
            ),
        }

    def profiles(self) -> list[dict[str, Any]]:
        return [item.public() for item in self._profiles.values()]

    def profile(self, agent_id: str) -> Profile | None:
        return self._profiles.get(agent_id)

    def models(self) -> dict[str, Any]:
        """server-default comes first so a client that picks the first option uses the private model."""
        return {
            "default_model": SERVER_DEFAULT_MODEL,
            "models": [
                {"id": SERVER_DEFAULT_MODEL, "label": _server_default_label()},
                *({"id": item, "label": item} for item in ALLOWED_MODELS),
            ],
        }

    def known_model(self, model: str) -> bool:
        if model in ALLOWED_MODELS or model == SERVER_DEFAULT_MODEL:
            return True
        return model.startswith("private:") or model.startswith("siliconflow:")
