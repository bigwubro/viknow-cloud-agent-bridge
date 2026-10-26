"""Immutable, non-secret behavior facts captured when an agent run starts."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.runtime.core.config import AgentModelConfig
from agents.runtime.core.runtime.domain import AgentOp, AgentProfile, KnowledgeAssetSelection
from agents.runtime.core.skills import SkillSpec
from agents.runtime.core.tools import ToolSpec


class ArtifactSnapshot(BaseModel):
    """A version reference for one Skill or Tool implementation artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    implementation: str | None = None
    state: Literal["captured", "unavailable"]
    unavailable_reason: str | None = None


class ProfileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    effective_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: str | None = None
    resolved: str | None = None
    api_key_env: str
    parameters: dict[str, object] = Field(default_factory=dict)


class KnowledgeStartState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str | None = None
    use_precompiled_knowledge: bool
    selected_knowledge_assets: list[KnowledgeAssetSelection] = Field(default_factory=list)
    index_state: Literal["not_yet_observed"] = "not_yet_observed"


class RunBehaviorSnapshot(BaseModel):
    """All static behavior inputs known before the first model step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    harness_policy_version: str = "h1-observe-only"
    profile: ProfileSnapshot
    agent_model: ModelSnapshot
    skills: list[ArtifactSnapshot]
    tools: list[ArtifactSnapshot]
    knowledge_start_state: KnowledgeStartState


class RunTraceLink(BaseModel):
    """A terminal run reference to Langfuse without copying trace contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["resolved", "unavailable"]
    provider: Literal["langfuse"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    service_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    environment: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    project_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    trace_name: str | None = None
    final_answer_event_sequence: int | None = Field(default=None, ge=1)
    root_output_synced: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def validate_source_identity(self) -> RunTraceLink:
        values = (self.service_id, self.environment, self.source_id, self.project_id)
        if any(value is not None for value in values) and not all(values):
            raise ValueError("trace source identity must be complete")
        if all(values) and self.provider != "langfuse":
            raise ValueError("trace source identity requires provider=langfuse")
        if self.state == "resolved" and self.trace_id is None:
            raise ValueError("resolved trace link requires trace_id")
        if self.state == "unavailable":
            if self.trace_id is not None:
                raise ValueError("unavailable trace link cannot carry trace_id")
            if not self.reason:
                raise ValueError("unavailable trace link requires reason")
        return self

    @classmethod
    def resolved(
        cls,
        *,
        trace_id: str,
        trace_name: str | None,
        final_answer_event_sequence: int | None,
        root_output_synced: bool,
        reason: str | None = None,
        service_id: str | None = None,
        environment: str | None = None,
        source_id: str | None = None,
        project_id: str | None = None,
    ) -> RunTraceLink:
        return cls(
            state="resolved",
            provider="langfuse" if source_id else None,
            service_id=service_id,
            environment=environment,
            source_id=source_id,
            project_id=project_id,
            trace_id=trace_id,
            trace_name=trace_name,
            final_answer_event_sequence=final_answer_event_sequence,
            root_output_synced=root_output_synced,
            reason=reason,
        )

    @classmethod
    def unavailable(
        cls,
        reason: str,
        *,
        final_answer_event_sequence: int | None = None,
        service_id: str | None = None,
        environment: str | None = None,
        source_id: str | None = None,
        project_id: str | None = None,
    ) -> RunTraceLink:
        return cls(
            state="unavailable",
            provider="langfuse" if source_id else None,
            service_id=service_id,
            environment=environment,
            source_id=source_id,
            project_id=project_id,
            final_answer_event_sequence=final_answer_event_sequence,
            reason=reason,
        )


def build_run_behavior_snapshot(
    *,
    profile: AgentProfile,
    agent_model: AgentModelConfig,
    skills: list[SkillSpec],
    tool_specs: list[ToolSpec],
    op: AgentOp,
    repo_root: Path,
) -> RunBehaviorSnapshot:
    """Capture static runtime inputs without resolving secret environment values."""

    return RunBehaviorSnapshot(
        profile=ProfileSnapshot(
            id=profile.id,
            effective_config_sha256=_json_sha256(profile.model_dump(mode="json")),
        ),
        agent_model=build_model_snapshot(agent_model, requested=op.agent_model),
        skills=[_skill_snapshot(skill, repo_root=repo_root) for skill in skills],
        tools=[_tool_snapshot(tool, repo_root=repo_root) for tool in tool_specs],
        knowledge_start_state=KnowledgeStartState(
            workspace_id=op.workspace_id,
            use_precompiled_knowledge=op.use_precompiled_knowledge,
            selected_knowledge_assets=op.selected_knowledge_assets,
        ),
    )


def build_model_snapshot(
    agent_model: AgentModelConfig,
    *,
    requested: str | None,
) -> ModelSnapshot:
    """Capture one already-resolved effective model route without any secret value."""

    return ModelSnapshot(
        requested=requested,
        resolved=agent_model.resolve_model(),
        api_key_env=agent_model.api_key_env,
        parameters={
            "provider": agent_model.provider.value,
            "provider_name": agent_model.provider_name,
            "base_url": agent_model.resolve_base_url(),
            "base_url_env": agent_model.base_url_env,
            "temperature": agent_model.temperature,
            "repetition_penalty": agent_model.repetition_penalty,
            "max_input_tokens": agent_model.max_input_tokens,
            "timeout_seconds": agent_model.timeout_seconds,
        },
    )


def _skill_snapshot(skill: SkillSpec, *, repo_root: Path) -> ArtifactSnapshot:
    try:
        return ArtifactSnapshot(
            id=skill.name,
            source_path=_source_path(skill.skill_file, repo_root=repo_root),
            sha256=_file_sha256(skill.skill_file),
            state="captured",
        )
    except OSError:
        return ArtifactSnapshot(
            id=skill.name,
            source_path=_source_path(skill.skill_file, repo_root=repo_root),
            state="unavailable",
            unavailable_reason="skill_file_unavailable",
        )


def _tool_snapshot(tool: ToolSpec, *, repo_root: Path) -> ArtifactSnapshot:
    implementation = f"{tool.handler.__module__}.{tool.handler.__qualname__}"
    source_file = inspect.getsourcefile(tool.handler)
    if source_file is None:
        return ArtifactSnapshot(
            id=tool.id,
            implementation=implementation,
            state="unavailable",
            unavailable_reason="source_file_unavailable",
        )
    path = Path(source_file)
    try:
        return ArtifactSnapshot(
            id=tool.id,
            implementation=implementation,
            source_path=_source_path(path, repo_root=repo_root),
            sha256=_file_sha256(path),
            state="captured",
        )
    except OSError:
        return ArtifactSnapshot(
            id=tool.id,
            implementation=implementation,
            source_path=_source_path(path, repo_root=repo_root),
            state="unavailable",
            unavailable_reason="source_file_unavailable",
        )


def _source_path(path: Path, *, repo_root: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return None


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()
