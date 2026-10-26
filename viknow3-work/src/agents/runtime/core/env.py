"""Local environment loading helpers."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class EnvConfigurationError(RuntimeError):
    """Raised when local environment configuration is incomplete."""


def load_local_env(repo_root: Path | None = None) -> None:
    """Load local .env files without overriding existing environment variables."""

    root = repo_root or Path(__file__).resolve().parents[3]
    for path in [root / ".env", root / ".env.local"]:
        if path.exists():
            load_env_file(path)


def resolve_deploy_env_path(config_path: Path | None = None) -> Path | None:
    """Return ``config/<env>/deploy.env`` next to the app.yaml being loaded."""

    path = config_path
    if path is None:
        raw = os.getenv("VIKNOW_CONFIG", "").strip()
        path = Path(raw) if raw else None
    if path is None:
        return None
    candidate = path.expanduser().resolve().parent / "deploy.env"
    if candidate.is_file():
        return candidate
    env_name = path.parent.name
    repo_candidate = Path(__file__).resolve().parents[3] / "config" / env_name / "deploy.env"
    if repo_candidate.is_file():
        return repo_candidate
    return None


def apply_deploy_env(config_path: Path | None = None) -> dict[str, str | None]:
    """Re-read deploy.env and overwrite process env for keys present in the file.

    Returns previous values for written keys (``None`` if the key was absent)
    so a failed reload can restore them.
    """

    deploy_path = resolve_deploy_env_path(config_path)
    previous: dict[str, str | None] = {}
    if deploy_path is None:
        return previous
    for key, value in parse_env_file(deploy_path).items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    return previous


def restore_env_values(previous: dict[str, str | None]) -> None:
    """Undo ``apply_deploy_env`` after a failed reload."""

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def load_deploy_env(config_path: Path | None = None) -> None:
    """Load per-environment topology defaults; existing process env wins."""

    deploy_path = resolve_deploy_env_path(config_path)
    if deploy_path is not None:
        load_env_file(deploy_path)


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def validate_deploy_env(
    config_path: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> None:
    """Validate required values in the selected environment's ``deploy.env``."""

    root = repo_root or Path(__file__).resolve().parents[3]
    selected_config = config_path
    if selected_config is None:
        raw = os.getenv("VIKNOW_CONFIG", "").strip()
        selected_config = Path(raw) if raw else root / "config" / "local" / "app.yaml"
        deploy_path = resolve_deploy_env_path(selected_config)
    else:
        candidate = selected_config.expanduser().resolve().parent / "deploy.env"
        deploy_path = candidate if candidate.is_file() else None
    if deploy_path is None:
        raise EnvConfigurationError(
            f"Missing deploy.env next to selected config: {selected_config}"
        )

    values = parse_env_file(deploy_path)
    contract_path = root / "config" / "env.contract.yaml"
    payload = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    required = {
        str(item["name"])
        for item in payload.get("variables", [])
        if isinstance(item, dict)
        and item.get("required") is True
        and item.get("process") is not False
        and item.get("name")
    }
    merged = dict(values)
    merged.update({name: value for name, value in os.environ.items() if value})
    missing = sorted(name for name in required if _is_placeholder_env_value(merged.get(name, "")))
    if missing:
        raise EnvConfigurationError(
            "Selected deploy.env is missing required values: " + ", ".join(missing)
        )


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple dotenv file into key/value pairs."""

    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def _is_placeholder_env_value(value: str) -> bool:
    normalized = value.strip()
    return not normalized or (
        normalized.startswith("[") and normalized.endswith("]") and len(normalized) > 2
    )
