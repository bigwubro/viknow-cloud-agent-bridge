"""Shared helpers for config/env.contract.yaml validation and auditing."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "config" / "env.contract.yaml"
CONFIG_ROOT = REPO_ROOT / "config"
WORKFLOW_DIR = REPO_ROOT / ".gitea" / "workflows"

_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_FIELD_KEYS = frozenset(
    {
        "api_key_env",
        "base_url_env",
        "identity_env",
        "secret_env",
        "callback_token_env",
    }
)
_SKIP_YAML_SCAN = frozenset({"env.contract.yaml", "materialize.registry.yaml"})


def load_contract() -> dict:
    payload = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    variables = payload["variables"]
    names = [item["name"] for item in variables]
    if len(names) != len(set(names)):
        raise ValueError("duplicate variable in env.contract.yaml")
    for item in variables:
        if item["tier"] not in {1, 2, 3}:
            raise ValueError(f"invalid tier for {item['name']}")
        if not isinstance(item["secret"], bool):
            raise ValueError(f"invalid secret flag for {item['name']}")
    return payload


def contract_names(payload: dict | None = None) -> set[str]:
    payload = payload or load_contract()
    return {item["name"] for item in payload["variables"]}


def contract_aliases(payload: dict | None = None) -> dict[str, str]:
    payload = payload or load_contract()
    aliases: dict[str, str] = {}
    for item in payload["variables"]:
        for alias in item.get("aliases") or []:
            aliases[alias] = item["name"]
    return aliases


def iter_config_yaml_files() -> list[Path]:
    files: list[Path] = []
    for path in CONFIG_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _SKIP_YAML_SCAN:
            continue
        if path.suffix not in {".yaml", ".yml"}:
            continue
        files.append(path)
    return files


def referenced_env_names(path: Path) -> set[str]:
    names: set[str] = set(_ENV_TOKEN_RE.findall(path.read_text(encoding="utf-8")))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    names.update(collect_env_field_values(raw))
    return names


def collect_env_field_values(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ENV_FIELD_KEYS and isinstance(item, str) and item:
                found.add(item)
            found.update(collect_env_field_values(item))
    elif isinstance(value, list):
        for item in value:
            found.update(collect_env_field_values(item))
    return found


def secret_variable_names(payload: dict | None = None) -> set[str]:
    payload = payload or load_contract()
    return {item["name"] for item in payload["variables"] if item["secret"]}
