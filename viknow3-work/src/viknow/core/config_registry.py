"""Load and audit the config materialization registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from viknow.core.env_contract import (
    contract_aliases,
    load_contract,
    referenced_env_names,
    secret_variable_names,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "config" / "materialize.registry.yaml"
ENVIRONMENTS = (".example", "local", "devcontainer", "test", "private")


@dataclass(frozen=True)
class MaterializeRegistry:
    raw: dict[str, Any]
    hyperrag_source_forbidden: dict[str, list[str]]
    actio_model_refs: dict[str, str]
    app_owns: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path | None = None) -> MaterializeRegistry:
        registry_path = path or REGISTRY_PATH
        payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        return cls(
            raw=payload,
            hyperrag_source_forbidden=dict(payload.get("hyperrag_source_forbidden") or {}),
            actio_model_refs=dict(payload.get("actio_model_refs") or {}),
            app_owns=dict(payload.get("app_owns") or {}),
        )


@dataclass
class ConfigAuditReport:
    environment: str
    config_dir: Path
    app_yaml: Path
    deploy_env_path: Path
    deploy_keys: list[str] = field(default_factory=list)
    referenced_env: list[str] = field(default_factory=list)
    missing_deploy_values: list[str] = field(default_factory=list)
    missing_secrets: list[str] = field(default_factory=list)
    hyperrag_violations: list[str] = field(default_factory=list)
    actio_ref_violations: list[str] = field(default_factory=list)
    materialize_ok: bool = False
    materialize_error: str | None = None
    materialized_llm_model: str | None = None
    materialized_embedding_model: str | None = None

    @property
    def ok(self) -> bool:
        return (
            not self.missing_deploy_values
            and not self.hyperrag_violations
            and not self.actio_ref_violations
            and self.materialize_ok
        )


def environment_config_dir(environment: str) -> Path:
    if environment not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {environment!r}; expected one of {ENVIRONMENTS}")
    return REPO_ROOT / "config" / environment


def find_hyperrag_source_violations(
    hyperrag_raw: dict[str, Any],
    registry: MaterializeRegistry | None = None,
) -> list[str]:
    registry = registry or MaterializeRegistry.load()
    forbidden = registry.hyperrag_source_forbidden
    violations: list[str] = []

    for key in forbidden.get("top_level") or []:
        if key in hyperrag_raw:
            violations.append(f"top-level {key!r}")

    models = hyperrag_raw.get("models") or {}
    if isinstance(models, dict):
        for key in forbidden.get("models") or []:
            if key in models:
                violations.append(f"models.{key}")

    env = hyperrag_raw.get("env") or {}
    if isinstance(env, dict):
        for key in forbidden.get("env") or []:
            if key in env:
                violations.append(f"env.{key}")

    return violations


def find_actio_model_ref_violations(
    app_raw: dict[str, Any],
    actio_raw: dict[str, Any],
    registry: MaterializeRegistry | None = None,
) -> list[str]:
    registry = registry or MaterializeRegistry.load()
    expected = registry.actio_model_refs
    violations: list[str] = []

    if actio_raw.get("models") is not None:
        violations.append("actio.yaml still defines models (move to app.yaml share.actio.models)")

    share = app_raw.get("share") or {}
    actio = share.get("actio") or {}
    models = actio.get("models") or {}
    if not isinstance(models, dict):
        return ["app.yaml share.actio.models missing"]
    for ref_name, expected_value in expected.items():
        actual = models.get(ref_name)
        if actual != expected_value:
            violations.append(
                f"share.actio.models.{ref_name}: expected {expected_value!r}, got {actual!r}"
            )
    return violations


def audit_environment_config(
    environment: str,
    *,
    registry: MaterializeRegistry | None = None,
    include_materialize: bool = True,
) -> ConfigAuditReport:
    from viknow.core.env import parse_env_file

    config_dir = environment_config_dir(environment)
    app_yaml = config_dir / "app.yaml"
    deploy_env_path = config_dir / "deploy.env"
    report = ConfigAuditReport(
        environment=environment,
        config_dir=config_dir,
        app_yaml=app_yaml,
        deploy_env_path=deploy_env_path,
    )

    if not app_yaml.is_file():
        report.materialize_error = f"missing {app_yaml}"
        return report

    app_raw = yaml.safe_load(app_yaml.read_text(encoding="utf-8")) or {}
    deploy_keys = parse_env_file(deploy_env_path) if deploy_env_path.is_file() else {}
    report.deploy_keys = sorted(deploy_keys)

    referenced: set[str] = set()
    for path in config_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            continue
        referenced.update(referenced_env_names(path))
    report.referenced_env = sorted(referenced)

    contract = load_contract()
    secret_names = secret_variable_names(contract)
    aliases = contract_aliases(contract)
    skip = secret_names | {"VIKNOW_CONFIG", "VIKNOW_CONFIG_RELOAD", "PORT"}
    needed = {aliases.get(name, name) for name in referenced} - skip
    report.missing_deploy_values = sorted(name for name in needed if name not in deploy_keys)

    for name in sorted(secret_names):
        if name not in referenced:
            continue
        if deploy_keys.get(name, "").strip():
            continue
        if _env_present(name):
            continue
        report.missing_secrets.append(name)

    hyperrag_path = config_dir / "hyperrag.yml"
    if hyperrag_path.is_file():
        hyperrag_raw = yaml.safe_load(hyperrag_path.read_text(encoding="utf-8")) or {}
        report.hyperrag_violations = find_hyperrag_source_violations(hyperrag_raw, registry)

    actio_path = config_dir / "actio.yaml"
    actio_raw: dict[str, Any] = {}
    if actio_path.is_file():
        actio_raw = yaml.safe_load(actio_path.read_text(encoding="utf-8")) or {}
    report.actio_ref_violations = find_actio_model_ref_violations(app_raw, actio_raw, registry)

    if not include_materialize:
        report.materialize_ok = True
        return report

    try:
        rendered = _try_materialize(app_yaml)
    except Exception as exc:  # pragma: no cover - surfaced in report
        report.materialize_error = str(exc)
        return report

    report.materialize_ok = True
    report.materialized_llm_model = rendered.get("llm_model")
    report.materialized_embedding_model = rendered.get("embedding_model")
    return report


def format_audit_report(report: ConfigAuditReport) -> str:
    lines = [
        f"environment: {report.environment}",
        f"config_dir: {report.config_dir.relative_to(REPO_ROOT)}",
        f"app.yaml: {report.app_yaml.relative_to(REPO_ROOT)}",
        f"deploy.env: {report.deploy_env_path.relative_to(REPO_ROOT)} "
        f"({len(report.deploy_keys)} keys)",
        "",
        "registry: config/materialize.registry.yaml",
        "env_contract: config/env.contract.yaml",
        "",
    ]
    if report.missing_deploy_values:
        lines.append("missing deploy.env values:")
        lines.extend(f"  - {name}" for name in report.missing_deploy_values)
        lines.append("")
    if report.missing_secrets:
        lines.append("missing secrets (fill deploy.env or inject from CI before deploy):")
        lines.extend(f"  - {name}" for name in report.missing_secrets)
        lines.append("")
    if report.hyperrag_violations:
        lines.append("hyperrag.yml duplicates app-owned fields (remove from source):")
        lines.extend(f"  - {item}" for item in report.hyperrag_violations)
        lines.append("")
    if report.actio_ref_violations:
        lines.append("actio/app model ref drift (fix or update materialize.registry.yaml):")
        lines.extend(f"  - {item}" for item in report.actio_ref_violations)
        lines.append("")
    if report.materialize_error:
        lines.append(f"materialize: FAILED — {report.materialize_error}")
    elif report.materialize_ok:
        lines.append("materialize: OK")
        if report.materialized_llm_model:
            lines.append(f"  llm_model: {report.materialized_llm_model}")
        if report.materialized_embedding_model:
            lines.append(f"  embedding_model: {report.materialized_embedding_model}")
    lines.append("")
    lines.append(f"status: {'OK' if report.ok else 'NEEDS FIX'}")
    return "\n".join(lines)


def _env_present(name: str) -> bool:
    import os

    value = os.getenv(name, "").strip()
    return bool(value)


def _try_materialize(app_yaml: Path) -> dict[str, Any]:
    import os
    from tempfile import TemporaryDirectory

    from viknow.core.config import load_app_config, materialize_hyperrag_config

    with TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        previous_config = os.environ.get("VIKNOW_CONFIG")
        os.environ["VIKNOW_CONFIG"] = str(app_yaml.resolve())
        try:
            config = load_app_config(app_yaml)
            config = config.model_copy(
                update={"app": config.app.model_copy(update={"state_dir": state_dir})}
            )
            rendered_path = materialize_hyperrag_config(config)
            return yaml.safe_load(rendered_path.read_text(encoding="utf-8")) or {}
        finally:
            if previous_config is None:
                os.environ.pop("VIKNOW_CONFIG", None)
            else:
                os.environ["VIKNOW_CONFIG"] = previous_config
