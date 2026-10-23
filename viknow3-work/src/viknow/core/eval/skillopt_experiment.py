"""Create immutable inputs for an isolated SkillOpt experiment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from viknow.core.runtime.domain import AgentProfile
from viknow.core.skills import discover_skills


class SkillOptExperimentManifest(BaseModel):
    """Paths and content identities needed to reproduce one optimization run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skillopt-experiment.v1"] = "viknow.skillopt-experiment.v1"
    experiment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    redis_database: int = Field(ge=1, le=15)
    target_skill_name: str
    profile_id: str
    base_config_path: Path
    source_skill_dir: Path
    source_skill_sha256: str
    source_skill_tree_sha256: str
    source_profile_path: Path
    source_profile_sha256: str
    experiment_root: Path
    experiment_skill_root: Path
    experiment_skill_dir: Path
    experiment_skill_sha256: str
    experiment_skill_tree_sha256: str
    target_support_files_sha256: str
    frozen_skill_sha256: dict[str, str]
    experiment_profile_dir: Path
    experiment_profile_path: Path
    experiment_profile_sha256: str

    @field_validator("target_skill_name")
    @classmethod
    def validate_target_skill_name(cls, value: str) -> str:
        return _validate_skill_name(value)

    @field_validator("frozen_skill_sha256")
    @classmethod
    def validate_frozen_skills(cls, value: dict[str, str]) -> dict[str, str]:
        for name, digest in value.items():
            _validate_skill_name(name)
            _validate_sha256(digest)
        return value

    @field_validator(
        "source_skill_sha256",
        "source_skill_tree_sha256",
        "source_profile_sha256",
        "experiment_skill_sha256",
        "experiment_skill_tree_sha256",
        "target_support_files_sha256",
        "experiment_profile_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


def directory_sha256(root: Path) -> str:
    """Hash relative file names and contents for a reproducible directory identity."""

    return _directory_sha256(root, excluded_paths=frozenset())


def _directory_sha256(root: Path, *, excluded_paths: frozenset[str]) -> str:

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"experiment skill cannot contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if relative.decode("utf-8") in excluded_paths:
            continue
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_skillopt_experiment(
    *,
    experiment_id: str,
    base_config_path: Path,
    source_skill_dir: Path,
    source_profile_path: Path,
    output_dir: Path,
    redis_database: int,
) -> SkillOptExperimentManifest:
    """Copy one target Skill and derived Profile into a new experiment directory."""

    base_config_path = _require_file(base_config_path)
    source_profile_path = _require_file(source_profile_path)
    source_skill_dir = _require_directory(source_skill_dir)
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(output_dir)
    if output_dir == source_skill_dir or output_dir.is_relative_to(source_skill_dir):
        raise ValueError("experiment output cannot be inside the source skill directory")

    discovered_skills = discover_skills([source_skill_dir.parent])
    skill = discovered_skills.get(source_skill_dir.name)
    if skill is None or skill.source_dir != source_skill_dir:
        raise ValueError(f"target skill cannot be discovered from {source_skill_dir}")
    profile = AgentProfile.model_validate(
        _load_yaml_mapping(source_profile_path, label="source profile")
    )
    if skill.name not in profile.enabled_skills:
        raise ValueError(f"profile {profile.id} does not enable target skill {skill.name}")
    missing_profile_skills = sorted(set(profile.enabled_skills) - set(discovered_skills))
    if missing_profile_skills:
        raise ValueError(
            f"profile {profile.id} has unavailable skills: {', '.join(missing_profile_skills)}"
        )

    if not 1 <= redis_database <= 15:
        raise ValueError("experiment Redis database must be between 1 and 15")
    source_skill_sha256 = _file_sha256(skill.skill_file)
    source_skill_tree_sha256 = directory_sha256(source_skill_dir)
    source_profile_sha256 = _file_sha256(source_profile_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = output_dir.parent / f".{output_dir.name}.{uuid4().hex}.tmp"
    try:
        experiment_skill_root = temporary_root / "skills"
        experiment_skill_dir = experiment_skill_root / skill.name
        experiment_profile_dir = temporary_root / "profiles"
        experiment_profile_path = experiment_profile_dir / f"{profile.id}.yaml"
        for enabled_skill_name in profile.enabled_skills:
            enabled_skill = discovered_skills[enabled_skill_name]
            shutil.copytree(
                enabled_skill.source_dir,
                experiment_skill_root / enabled_skill_name,
            )
        experiment_profile_dir.mkdir(parents=True)
        experiment_profile_path.write_text(
            yaml.safe_dump(
                profile.model_dump(mode="json", exclude_none=True),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        final_skill_root = output_dir / "skills"
        final_skill_dir = final_skill_root / skill.name
        final_profile_dir = output_dir / "profiles"
        final_profile_path = final_profile_dir / f"{profile.id}.yaml"
        manifest = SkillOptExperimentManifest(
            experiment_id=experiment_id,
            redis_database=redis_database,
            target_skill_name=skill.name,
            profile_id=profile.id,
            base_config_path=base_config_path,
            source_skill_dir=source_skill_dir,
            source_skill_sha256=source_skill_sha256,
            source_skill_tree_sha256=source_skill_tree_sha256,
            source_profile_path=source_profile_path,
            source_profile_sha256=source_profile_sha256,
            experiment_root=output_dir,
            experiment_skill_root=final_skill_root,
            experiment_skill_dir=final_skill_dir,
            experiment_skill_sha256=_file_sha256(experiment_skill_dir / "SKILL.md"),
            experiment_skill_tree_sha256=directory_sha256(experiment_skill_dir),
            target_support_files_sha256=_directory_sha256(
                experiment_skill_dir,
                excluded_paths=frozenset({"SKILL.md"}),
            ),
            frozen_skill_sha256={
                name: directory_sha256(experiment_skill_root / name)
                for name in profile.enabled_skills
                if name != skill.name
            },
            experiment_profile_dir=final_profile_dir,
            experiment_profile_path=final_profile_path,
            experiment_profile_sha256=_file_sha256(experiment_profile_path),
        )
        (temporary_root / "manifest.json").write_text(
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.rename(temporary_root, output_dir)
        return manifest
    except BaseException:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise


def load_skillopt_experiment_manifest(path: Path) -> SkillOptExperimentManifest:
    """Load a typed experiment manifest without resolving any credentials."""

    manifest_path = _require_file(path)
    manifest = SkillOptExperimentManifest.model_validate_json(manifest_path.read_bytes())
    if manifest_path != (manifest.experiment_root / "manifest.json").resolve():
        raise ValueError("manifest path must be experiment_root/manifest.json")
    return manifest


def validate_skillopt_experiment_copy(
    manifest: SkillOptExperimentManifest,
    *,
    expected_target_skill_sha256: str,
) -> None:
    root = _require_directory(manifest.experiment_root)
    canonical_paths = {
        "experiment_skill_root": root / "skills",
        "experiment_skill_dir": root / "skills" / manifest.target_skill_name,
        "experiment_profile_dir": root / "profiles",
        "experiment_profile_path": root / "profiles" / f"{manifest.profile_id}.yaml",
    }
    for field_name, expected_path in canonical_paths.items():
        actual_path = getattr(manifest, field_name).expanduser().resolve()
        if actual_path != expected_path:
            raise ValueError(f"non-canonical experiment path: {field_name}")
    skill_file = _require_file(manifest.experiment_skill_dir / "SKILL.md")
    if _file_sha256(skill_file) != expected_target_skill_sha256:
        raise ValueError("experiment Skill hash mismatch")
    if expected_target_skill_sha256 == manifest.experiment_skill_sha256 and (
        directory_sha256(manifest.experiment_skill_dir) != manifest.experiment_skill_tree_sha256
    ):
        raise ValueError("experiment Skill tree hash mismatch")
    if (
        _directory_sha256(
            manifest.experiment_skill_dir,
            excluded_paths=frozenset({"SKILL.md"}),
        )
        != manifest.target_support_files_sha256
    ):
        raise ValueError("experiment target Skill support files hash mismatch")
    for skill_name, expected_hash in manifest.frozen_skill_sha256.items():
        actual_hash = directory_sha256(manifest.experiment_skill_root / skill_name)
        if actual_hash != expected_hash:
            raise ValueError(f"frozen experiment Skill hash mismatch: {skill_name}")
    if _file_sha256(_require_file(manifest.experiment_profile_path)) != (
        manifest.experiment_profile_sha256
    ):
        raise ValueError("experiment Profile hash mismatch")


def _require_file(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _require_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_skill_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise ValueError(f"unsafe skill name: {value!r}")
    return value


def _validate_sha256(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected a lowercase SHA-256 digest")
    return value


def _load_yaml_mapping(path: Path, *, label: str) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must contain a mapping: {path}")
    return raw
