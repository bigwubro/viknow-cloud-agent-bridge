"""Typed server configuration for ViKnow."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from agents.runtime.capabilities.config import CapabilitiesConfig
from agents.runtime.core.env import parse_env_file
from agents.runtime.core.model_gateway import ModelGatewayRoute
from agents.runtime.core.profiles import AgentProfileCatalog, load_profile_catalog
from agents.runtime.core.scheduler.unified_config import (
    SchedulerSectionConfig,
    default_scheduler_section_config,
)
from agents.runtime.core.trace_console.models import TraceConsoleConfig
from agents.runtime.deployment.license.config import LicenseConfig


class ModelProvider(StrEnum):
    """Supported model provider families."""

    OPENAI_COMPATIBLE = "openai-compatible"
    PADDLEOCR = "paddleocr"
    GRANITE = "granite"
    ARK = "ark"


class AgentKernelType(StrEnum):
    """Supported agent runtime backends."""

    DEEPAGENTS = "deepagents"
    HERMES = "hermes"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_VAR_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
FILES_ROOT_ENV = "VIKNOW_FILES_ROOT"
FILES_ROOT_SESSIONS_DIR = "sessions"
FILES_ROOT_KNOWLEDGE_DIR = "knowledge"
FILES_ROOT_KNOWLEDGE_UPLOADS_DIR = "uploads"
FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR = "libraries"
FILES_ROOT_KNOWLEDGE_EXPORTS_DIR = "exports"
ACTIO_MODEL_REF_PROVIDER = "actio"
DEFAULT_ACTIO_CONFIG_DIR = Path("actio/configs")
_ACTIO_ASR_PASSTHROUGH_KEYS = (
    "provider",
    "region_mode",
    "transport",
    "max_upload_mb",
    "chunk_seconds",
    "timeout_seconds",
    "job_timeout_seconds",
    "preparation_timeout_seconds",
    "queue_timeout_seconds",
    "transcription_timeout_seconds",
    "poll_interval_seconds",
    "response_format",
)


class ModelProviderConfig(_StrictModel):
    """Named OpenAI-compatible model API source."""

    provider: ModelProvider = ModelProvider.OPENAI_COMPATIBLE
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    reasoning_content_roundtrip: bool = False
    fallback_model_refs: dict[str, str] = Field(default_factory=dict)
    gateway_max_recovery_attempts: int = Field(default=0, ge=0, le=2)
    gateway_recovery_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    gateway_require_gateway: bool = False
    # V5 fix 2: opt-in capacity-only recovery escalation; off keeps V4-B behaviour.
    gateway_capacity_recovery_enabled: bool = False

    def resolve_base_url(self) -> str | None:
        return _config_value("base_url", self.base_url, self.base_url_env) or None

    def resolve_api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None

    def require_api_key(self) -> str:
        value = self.resolve_api_key()
        if value:
            return value
        raise RuntimeError(f"{self.api_key_env} is required by model_providers")


AgentModelProviderConfig = ModelProviderConfig


class AgentModelConfig(BaseModel):
    """Model settings shared by agent adapters."""

    model_config = ConfigDict(extra="forbid")

    ref: str | None = Field(default=None, min_length=1)
    provider: ModelProvider = ModelProvider.OPENAI_COMPATIBLE
    provider_name: str | None = Field(default=None, min_length=1)
    model: str | None = None
    model_env: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    reasoning_content_roundtrip: bool = False
    # Optional request-level thinking switch.  ``None`` preserves the
    # provider default; it is intentionally not enabled for normal QA.
    enable_thinking: bool | None = None
    temperature: float = Field(default=0, ge=0)
    repetition_penalty: float | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    gateway: ModelGatewayRoute | None = None

    def resolve_model(self) -> str | None:
        if self.ref:
            return model_name_from_ref(self.ref, required=False)
        return _config_value("model", self.model, self.model_env)

    def require_model(self) -> str:
        value = self.resolve_model()
        if value:
            return value
        raise RuntimeError("agent.model.model or agent.model.model_env is required")

    def resolve_base_url(self) -> str | None:
        return _config_value("base_url", self.base_url, self.base_url_env) or None

    def require_api_key(self) -> str:
        value = os.getenv(self.api_key_env)
        if value:
            return value
        raise RuntimeError(f"{self.api_key_env} is required by agent.model.api_key_env")

    def selection_id(self) -> str | None:
        if self.ref:
            return normalized_model_ref(self.ref, required=False)
        model = self.resolve_model()
        if not model:
            return None
        if self.provider_name:
            return f"{self.provider_name}:{model}"
        return model


class PersistenceBackend(StrEnum):
    """Shared storage backend for session, checkpointer, and mem0 vectors."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


DEFAULT_SESSION_DSN_ENV = "VIKNOW_SESSION_DSN"
DEFAULT_CHECKPOINT_DSN_ENV = "VIKNOW_CHECKPOINT_DSN"
DEFAULT_MEMORY_DSN_ENV = "VIKNOW_MEMORY_DSN"
DEFAULT_PERSISTENCE_BACKEND_ENV = "VIKNOW_PERSISTENCE_BACKEND"


class PostgresPersistenceConfig(_StrictModel):
    """Postgres settings for session / checkpointer / mem0 / scheduler schemas.

    Connection fields (``host`` / ``port`` / ``user`` / ``password`` / ``database``)
    default from ``hyperrag.yml`` ``env.POSTGRES_*`` when omitted. Schema names are
    ViKnow-owned and stay in ``app.yaml``.

    ``juicefs_schema`` holds JuiceFS metadata only (created by JuiceFS
    entrypoint / optional postgres schema bootstrap). The application never
    reads or writes tables inside it.
    """

    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    upload_statement_timeout_seconds: float = Field(default=60, gt=0)
    session_schema: str = "viknow_session"
    checkpoint_schema: str = "viknow_checkpoint"
    memory_schema: str = "viknow_memory"
    scheduler_schema: str = "viknow_scheduler"
    learning_schema: str = "viknow_learning"
    juicefs_schema: str = "viknow_juicefs"


class AdminConfig(_StrictModel):
    """Admin console credentials from ``app.yaml`` (``admin.username`` / ``admin.password``)."""

    username: str | None = None
    password: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)


class PersistenceConfig(_StrictModel):
    """App persistence settings.

    Yaml typically sets ``backend: ${VIKNOW_PERSISTENCE_BACKEND}``. Postgres
    connection / schema fields live under ``postgres``; connection fields may be
    inherited from HyperRAG ``env.POSTGRES_*`` when omitted.
    """

    backend: PersistenceBackend | None = None
    postgres: PostgresPersistenceConfig | None = None


class LearningProjectionConfig(_StrictModel):
    """Controls the optional production Learning Projection pipeline.

    Both switches default to false so deploying this configuration never changes
    Q&A behavior or starts collecting production learning samples implicitly.
    """

    enabled: bool = False
    auto_project_completed_runs: bool = False
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    batch_size: int = Field(default=20, ge=1, le=100)
    max_attempts: int = Field(default=0, ge=0)


class CheckpointerConfig(_StrictModel):
    """Persistent conversation state settings.

    ``backend`` / ``sqlite_path`` / ``postgres_dsn`` are populated from
    ``AppConfig.persistence`` at load time; do not set under ``agent.deepagents``.
    """

    enabled: bool = True
    sqlite_path: Path | None = None
    backend: PersistenceBackend = PersistenceBackend.SQLITE
    postgres_dsn: str | None = None


class WorkspaceBackendType(StrEnum):
    """DeepAgents backend provider for workspace operations."""

    FILESYSTEM = "filesystem"
    BUBBLEWRAP = "bubblewrap"


DEFAULT_PARSE_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    "txt",
    "md",
    "markdown",
    "html",
    "htm",
    "json",
    "docx",
    "pdf",
    "pptx",
    "xls",
    "xlsx",
    "csv",
    "jpg",
    "jpeg",
    "png",
    "bmp",
    "tiff",
    "tif",
    "webp",
    "gif",
    "zip",
)


class WorkspaceStagingConfig(_StrictModel):
    """Local staging cleanup policy after a run (scheme B)."""

    keep_uploads_cache: bool = True
    purge_outputs_after_sync: bool = True
    purge_scratch_after_run: bool = True


class WorkspaceUploadConfig(_StrictModel):
    """Upload limits. Empty allowed_extensions means any type (Yuxi-style)."""

    max_file_mb: int = Field(default=25, ge=1)
    allowed_extensions: list[str] = Field(default_factory=list)


class WorkspaceParseConfig(_StrictModel):
    """Optional post-upload parse (narrow-out). Disabled by default in P0."""

    enabled: bool = False
    allowed_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PARSE_ALLOWED_EXTENSIONS)
    )


class WorkspaceConfig(_StrictModel):
    """Workspace settings used by DeepAgents backends and FilesystemManager."""

    root_dir: Path | None = None
    backend: WorkspaceBackendType = WorkspaceBackendType.FILESYSTEM
    virtual_mode: bool = True
    max_file_size_mb: int = Field(default=25, ge=1)
    # Legacy MinIO object-key prefix; kept for reading pre-files_root session_files rows.
    prefix: str = "workspaces"
    staging: WorkspaceStagingConfig = Field(default_factory=WorkspaceStagingConfig)
    upload: WorkspaceUploadConfig = Field(default_factory=WorkspaceUploadConfig)
    parse: WorkspaceParseConfig = Field(default_factory=WorkspaceParseConfig)


class SandboxConfig(_StrictModel):
    """Bubblewrap sandbox limits.

    These settings are only used when ``workspace.backend`` is
    ``bubblewrap``. The filesystem backend is intentionally separate so
    local development and unit tests can exercise workspace semantics without
    pretending to provide OS-level isolation.
    """

    memory_limit_mb: int = Field(default=512, ge=64)
    max_pids: int = Field(default=256, ge=16)
    cpu_shares: int = Field(default=100, ge=2)
    timeout_seconds: float = Field(default=60, gt=0)
    max_output_bytes: int = Field(default=262144, ge=1024)
    network_access: bool = False
    gpu: bool = False
    extra_bind_mounts: list[tuple[str, str]] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)


class SkillReminderConfig(_StrictModel):
    """Short per-turn reminder that helps models route into matching skills."""

    enabled: bool = False


class SummarizationConfig(_StrictModel):
    """DeepAgents conversation summarization thresholds."""

    trigger_fraction: float = Field(default=0.6, gt=0, le=1)
    keep_fraction: float = Field(default=0.05, gt=0, le=1)


class MultiAgentConfig(_StrictModel):
    """Opt-in DeepAgents specialist delegation for one environment."""

    enabled: bool = False


class DeepAgentsKernelConfig(_StrictModel):
    """Configuration for the DeepAgents-backed ViKnow kernel."""

    state_root: Path | None = None
    checkpointer: CheckpointerConfig = Field(default_factory=CheckpointerConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    recursion_limit: int = Field(default=999, ge=1)
    stream_events_version: Literal["v2", "v3"] = "v2"
    skill_reminder: SkillReminderConfig = Field(default_factory=SkillReminderConfig)
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)

    @field_validator("state_root", mode="after")
    @classmethod
    def expand_state_root(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None


class HermesKernelConfig(_StrictModel):
    """Hermes kernel settings."""

    home: Path | None = None


class ToolModelConfig(_StrictModel):
    """OpenAI-compatible model settings for ViKnow-owned tools."""

    model_error_subject: ClassVar[str] = "tool"
    api_key_error_subject: ClassVar[str] = "tool model config"

    ref: str | None = Field(default=None, min_length=1)
    provider: ModelProvider = ModelProvider.OPENAI_COMPATIBLE
    model: str | None = None
    model_env: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str
    reasoning_content_roundtrip: bool = False
    timeout_seconds: float = Field(default=60, gt=0)
    gateway: ModelGatewayRoute | None = None

    def require_model(self) -> str:
        if self.ref:
            return model_name_from_ref(self.ref, required=True) or ""
        value = _config_value("model", self.model, self.model_env)
        if value:
            return value
        raise RuntimeError(f"{self.model_error_subject} model or model_env is required")

    def resolve_base_url(self) -> str | None:
        return _config_value("base_url", self.base_url, self.base_url_env) or None

    def resolve_api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None

    def require_api_key(self) -> str:
        value = self.resolve_api_key()
        if value:
            return value
        raise RuntimeError(f"{self.api_key_env} is required by {self.api_key_error_subject}")


class KnowledgeToolConfig(_StrictModel):
    """Configuration for knowledge retrieval tools."""

    index_dir: Path
    spec_path: Path


class MultimodalReviewModelConfig(ToolModelConfig):
    """Multimodal video inspection model settings."""

    model_env: str | None = "VIKNOW_PRIVATE_MODEL"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)


class ASRToolConfig(ToolModelConfig):
    """Remote ASR service settings."""

    model_error_subject: ClassVar[str] = "asr"
    api_key_error_subject: ClassVar[str] = "asr tool config"

    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    timeout_seconds: float = Field(default=120, gt=0)
    region_mode: str = "vad"
    transport: Literal["auto", "path", "upload"] = "auto"
    max_upload_mb: float = Field(default=128, gt=0)
    chunk_seconds: float = Field(default=1800, gt=0)
    job_timeout_seconds: float = Field(default=7200, gt=0, allow_inf_nan=False)
    preparation_timeout_seconds: float = Field(default=1800, gt=0, allow_inf_nan=False)
    queue_timeout_seconds: float = Field(default=1800, gt=0, allow_inf_nan=False)
    transcription_timeout_seconds: float = Field(default=3600, gt=0, allow_inf_nan=False)
    poll_interval_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    vad_enabled: bool = True
    vad_frame_ms: int = Field(default=30, gt=0)
    vad_min_region_s: float = Field(default=0.4, gt=0)
    vad_merge_gap_s: float = Field(default=0.25, ge=0)
    vad_padding_s: float = Field(default=0.15, ge=0)
    vad_energy_threshold: int = Field(default=300, gt=0)


class CaptionToolConfig(ToolModelConfig):
    """Remote video caption service settings."""

    model_error_subject: ClassVar[str] = "caption"
    api_key_error_subject: ClassVar[str] = "caption tool config"

    model_env: str | None = "VIKNOW_PRIVATE_MODEL"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)


class OCRToolConfig(ToolModelConfig):
    """Connection settings for one OCR backend."""

    model_error_subject: ClassVar[str] = "ocr"
    api_key_error_subject: ClassVar[str] = "ocr tool config"

    model_env: str | None = "VIKNOW_PRIVATE_MODEL"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)
    video_frame_fps: float = Field(default=2.0, gt=0)


class VideoDiscoveryToolConfig(ToolModelConfig):
    """Remote video discovery service settings."""

    model_error_subject: ClassVar[str] = "video discovery"
    api_key_error_subject: ClassVar[str] = "video discovery tool config"

    model_env: str | None = "VIKNOW_PRIVATE_MODEL"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)


class LocateAnythingToolConfig(_StrictModel):
    """Actio LocateAnything service settings."""

    ref: str | None = Field(default=None, min_length=1)
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)

    def resolve_base_url(self) -> str | None:
        return _config_value("base_url", self.base_url, self.base_url_env) or None

    def resolve_api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None

    def require_api_key(self) -> str:
        value = self.resolve_api_key()
        if value:
            return value
        raise RuntimeError(f"{self.api_key_env} is required by locate_anything tool config")


class EvalJudgeModelConfig(ToolModelConfig):
    """Model settings for development eval judges."""

    model_error_subject: ClassVar[str] = "eval judge"
    api_key_error_subject: ClassVar[str] = "eval judge model config"

    model_env: str | None = "VIKNOW_PRIVATE_MODEL"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)
    temperature: float = Field(default=0, ge=0)
    max_tokens: int = Field(default=4096, gt=0)
    enable_thinking: bool = False


class InspectToolEvalConfig(_StrictModel):
    """Development eval settings for inspect tools."""

    judge_model: EvalJudgeModelConfig = Field(default_factory=EvalJudgeModelConfig)

    @field_validator("judge_model", mode="before")
    @classmethod
    def expand_ref_shorthand(cls, value: object) -> object:
        if isinstance(value, str):
            return {"ref": value}
        return value


class SkillFlowEvalConfig(_StrictModel):
    """Development settings for offline Skill Flow evaluation."""

    oracle_judge_model: EvalJudgeModelConfig = Field(default_factory=EvalJudgeModelConfig)
    analysis_model: EvalJudgeModelConfig = Field(default_factory=EvalJudgeModelConfig)

    @field_validator("oracle_judge_model", "analysis_model", mode="before")
    @classmethod
    def expand_ref_shorthand(cls, value: object) -> object:
        if isinstance(value, str):
            return {"ref": value}
        return value


class EvalConfig(_StrictModel):
    """Development eval configuration."""

    inspect_tool: InspectToolEvalConfig = Field(default_factory=InspectToolEvalConfig)
    skill_flow: SkillFlowEvalConfig = Field(default_factory=SkillFlowEvalConfig)


class SelfLearningAnalysisConfig(_StrictModel):
    """Durable online semantic-analysis worker settings."""

    enabled: bool = False
    model: EvalJudgeModelConfig = Field(default_factory=EvalJudgeModelConfig)
    # Analysis is an isolated runtime, so reasoning can be enabled here
    # without changing the model used by ordinary conversations.
    thinking_enabled: bool = True
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    batch_size: int = Field(default=4, ge=1, le=100)
    lease_seconds: float = Field(default=1200.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    target_skill_id: str = "knowledge-qa-v2"

    @field_validator("model", mode="before")
    @classmethod
    def expand_ref_shorthand(cls, value: object) -> object:
        if isinstance(value, str):
            return {"ref": value}
        return value


class SelfLearningOracleConfig(_StrictModel):
    """Durable Evidence Oracle worker settings."""

    enabled: bool = False
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    batch_size: int = Field(default=1, ge=1, le=10)
    lease_seconds: float = Field(default=2100.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_seconds: float = Field(default=1800.0, gt=0)

    @model_validator(mode="after")
    def validate_lease_covers_execution(self) -> SelfLearningOracleConfig:
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError("Oracle lease_seconds must exceed timeout_seconds")
        return self


class SelfLearningSkillOptimizationConfig(_StrictModel):
    """Durable SkillOpt worker scheduling settings."""

    enabled: bool = False
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    batch_size: int = Field(default=1, ge=1, le=4)
    lease_seconds: float = Field(default=2100.0, gt=0)
    max_attempts: int = Field(default=1, ge=1, le=3)
    timeout_seconds: float = Field(default=1800.0, gt=0)
    skillopt_package_root: Path | None = None
    skillopt_config_path: Path | None = None
    experiment_root: Path | None = None
    base_config_path: Path | None = None
    source_skill_root: Path | None = None
    source_profile_path: Path | None = None
    redis_database: int = Field(default=14, ge=1, le=15)
    optimizer_model_env: str = "VIKNOW_PRIVATE_MODEL"
    optimizer_base_url_env: str = "VIKNOW_PRIVATE_BASE_URL"
    optimizer_api_key_env: str = "VIKNOW_PRIVATE_API_KEY"

    @model_validator(mode="after")
    def validate_lease_covers_execution(self) -> SelfLearningSkillOptimizationConfig:
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError("SkillOpt lease_seconds must exceed timeout_seconds")
        if self.enabled and any(
            path is None
            for path in (
                self.skillopt_package_root,
                self.skillopt_config_path,
                self.experiment_root,
                self.base_config_path,
                self.source_skill_root,
                self.source_profile_path,
            )
        ):
            raise ValueError("enabled SkillOpt worker requires all filesystem paths")
        return self


def skill_optimization_model_options(config: AppConfig) -> list[dict[str, str]]:
    """Expose the existing server model directory without connection settings."""
    return [
        {"id": option.selection_id(), "label": option.label or option.selection_id()}
        for option in config.agent.model_options
    ]


def resolve_skill_optimization_model(config: AppConfig, selection_id: str) -> AgentModelConfig:
    for option in config.agent.model_options:
        if option.selection_id() == selection_id:
            if option.ref:
                return resolve_model_config(
                    AgentModelConfig(ref=option.ref), config.model_providers
                )
            base = resolve_model_config(config.agent.model, config.model_providers)
            fields = option.model_dump(exclude={"id", "label", "ref"}, exclude_none=True)
            fields.update(
                ref=None,
                model=option.model or (selection_id if option.model_env is None else None),
                model_env=option.model_env,
            )
            if option.provider_name:
                base = resolve_model_config(
                    AgentModelConfig(provider_name=option.provider_name), config.model_providers
                )
            if option.base_url_env:
                fields["base_url"] = None
            # Provider defaults are resolved before explicit option overrides.
            return base.model_copy(update=fields)
    raise ValueError("unknown SkillOpt model selection")


class VicoachModelConfig(ToolModelConfig):
    """One shared local LLM configuration for ViCoach generation and scoring."""

    model_error_subject: ClassVar[str] = "vicoach"
    api_key_error_subject: ClassVar[str] = "vicoach model config"

    ref: str | None = None
    model_env: str | None = "VIKNOW_PRIVATE_MODEL"
    api_key_env: str = "VIKNOW_PRIVATE_API_KEY"
    base_url_env: str | None = "VIKNOW_PRIVATE_BASE_URL"
    timeout_seconds: float = Field(default=180, gt=0)
    temperature: float = Field(default=0, ge=0)


class VicoachConfig(_StrictModel):
    """Configuration for the ViCoach question and answer workflow."""

    model: VicoachModelConfig = Field(default_factory=VicoachModelConfig)
    question_generation_temperature: float = Field(default=0.7, gt=0, le=2)
    question_generation_concurrency: int = Field(default=10, ge=1, le=100)

    @field_validator("model", mode="before")
    @classmethod
    def expand_ref_shorthand(cls, value: object) -> object:
        if isinstance(value, str):
            return {"ref": value}
        return value


class ActioModelsConfig(_StrictModel):
    """Actio model refs; source lives under ``app.yaml`` → ``share.actio.models``."""

    vlm: str = "private:${VIKNOW_PRIVATE_MODEL}"
    llm: str = "private:${VIKNOW_PRIVATE_MODEL}"
    embed: str = "embedding:${VIKNOW_EMBEDDING_MODEL}"


class ShareActioConfig(_StrictModel):
    models: ActioModelsConfig = Field(default_factory=ActioModelsConfig)
    vlm_timeout_seconds: float = Field(default=180, gt=0)


class ShareAgentConfig(_StrictModel):
    """Default agent chat model; omitted → ``share.actio.models.llm``."""

    model_ref: str | None = None
    fallback_model_ref: str | None = None


class ShareMemoryConfig(_StrictModel):
    """mem0 embedder/llm refs; omitted → ``share.actio.models.embed`` / ``llm``."""

    embedder_ref: str | None = None
    llm_ref: str | None = None


class ActioIntegrationConfig(_StrictModel):
    """ViKnow-owned source config for rendering actio model calls."""

    models_config_path: Path | None = None
    segmentation_config: Path | None = None
    ocr_config: Path | None = None
    aggregate_config: Path | None = None
    merge_config: Path | None = None
    merge_dtw_config: Path | None = None
    sop_schema_path: Path | None = None
    device: str | None = None
    models: dict[str, object] | None = None
    toolkit: dict[str, Any] | None = None


class MultimodalToolConfig(_StrictModel):
    """Configuration for multimodal evidence tools."""

    review_model: MultimodalReviewModelConfig
    asr: ASRToolConfig | None = None
    caption: CaptionToolConfig | None = None
    ocr: OCRToolConfig | None = None
    video_discovery: VideoDiscoveryToolConfig | None = None
    locate_anything: LocateAnythingToolConfig | None = None

    @field_validator(
        "review_model",
        "asr",
        "caption",
        "ocr",
        "video_discovery",
        "locate_anything",
        mode="before",
    )
    @classmethod
    def expand_ref_shorthand(cls, value: object) -> object:
        if isinstance(value, str):
            return {"ref": value}
        return value


class ActioToolConfig(_StrictModel):
    """Configuration owned by ViKnow for actio pipeline calls."""

    models_config_path: Path


class ToolConfigs(_StrictModel):
    """All environment-scoped ViKnow tool configs."""

    knowledge: KnowledgeToolConfig | None = None
    multimodal: MultimodalToolConfig | None = None
    actio: ActioToolConfig | None = None
    monitoring: MonitoringApiConfig | None = None


class KnowledgeEngineType(StrEnum):
    """Supported knowledge backend engines."""

    HYPERRAG = "hyperrag"


class ShareDatabasePostgresConfig(_StrictModel):
    """Shared Postgres connection topology (schemas stay under ``persistence.postgres``)."""

    host: str | None = None
    port: str | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None


class ShareJuicefsPostgresConfig(_StrictModel):
    """JuiceFS metadata Postgres connection (optional override of business ``postgres``)."""

    host: str | None = None
    port: str | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None


class ShareDatabaseConfig(_StrictModel):
    postgres: ShareDatabasePostgresConfig | None = None
    juicefs_postgres: ShareJuicefsPostgresConfig | None = None


class ShareLangfuseConfig(_StrictModel):
    """Shared Langfuse credentials materialized into HyperRAG runtime env."""

    host: str = "${VIKNOW_LANGFUSE_HOST}"
    secret_key: str = "${VIKNOW_LANGFUSE_SECRET_KEY}"
    public_key: str = "${VIKNOW_LANGFUSE_PUBLIC_KEY}"
    media_upload_enabled: str = "true"
    tracing_enabled: str = "true"


class ShareRedisConfig(_StrictModel):
    """Shared Redis connection (HyperRAG task queue + unified scheduler)."""

    host: str = "${REDIS_HOST}"
    port: str = "${REDIS_PORT}"
    username: str = "${REDIS_USERNAME}"
    password: str = "${REDIS_PASSWORD}"
    database: str = "${REDIS_DATABASE}"


def build_redis_url(
    *,
    host: str | None,
    port: str | int | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | int | None = None,
) -> str:
    """Build a Redis URI from discrete connection fields."""

    host_text = str(host).strip() if host is not None else ""
    if not host_text:
        return ""
    port_text = str(port).strip() if port is not None and str(port).strip() else ""
    db_text = str(database).strip() if database is not None and str(database).strip() else "0"
    user_text = str(username).strip() if username is not None else ""
    pwd_text = str(password).strip() if password is not None else ""

    auth = ""
    if user_text or pwd_text:
        user_enc = quote_plus(user_text) if user_text else ""
        pwd_enc = quote_plus(pwd_text) if pwd_text else ""
        if user_enc and pwd_enc:
            auth = f"{user_enc}:{pwd_enc}@"
        elif pwd_enc:
            auth = f":{pwd_enc}@"
        elif user_enc:
            auth = f"{user_enc}@"

    port_suffix = f":{port_text}" if port_text else ""
    return f"redis://{auth}{host_text}{port_suffix}/{db_text}"


def _resolve_share_redis_url(redis: ShareRedisConfig) -> str:
    def expand(value: str | None, field_name: str) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        token_match = _ENV_TOKEN_RE.fullmatch(text)
        if token_match is not None:
            env_value = os.getenv(token_match.group(1), "").strip()
            if not env_value:
                return ""
            return env_value
        expanded = _expand_env_tokens(text, field_name=field_name, required=False)
        return expanded or text

    return build_redis_url(
        host=expand(redis.host, "share.redis.host"),
        port=expand(redis.port, "share.redis.port"),
        username=expand(redis.username, "share.redis.username"),
        password=expand(redis.password, "share.redis.password"),
        database=expand(redis.database, "share.redis.database"),
    )


class ShareNeo4jConfig(_StrictModel):
    """Shared Neo4j graph store connection for HyperRAG."""

    uri: str = "${NEO4J_URI}"
    username: str = "${NEO4J_USERNAME}"
    password: str = "${NEO4J_PASSWORD}"


class ShareToolsRuntimeConfig(_StrictModel):
    """Process-wide ViKnow custom tool execution settings."""

    thread_pool_size: int = Field(default=64, ge=1, le=512)


class ShareConfig(_StrictModel):
    """Cross-component settings shared by ViKnow app, HyperRAG, and Actio within one env pack."""

    model_providers: dict[str, ModelProviderConfig] = Field(default_factory=dict)
    actio: ShareActioConfig = Field(default_factory=ShareActioConfig)
    agent: ShareAgentConfig = Field(default_factory=ShareAgentConfig)
    memory: ShareMemoryConfig = Field(default_factory=ShareMemoryConfig)
    database: ShareDatabaseConfig = Field(default_factory=ShareDatabaseConfig)
    redis: ShareRedisConfig = Field(default_factory=ShareRedisConfig)
    neo4j: ShareNeo4jConfig = Field(default_factory=ShareNeo4jConfig)
    langfuse: ShareLangfuseConfig = Field(default_factory=ShareLangfuseConfig)
    files_root: Path | None = None
    tools: ShareToolsRuntimeConfig = Field(default_factory=ShareToolsRuntimeConfig)
    scheduler: SchedulerSectionConfig = Field(default_factory=default_scheduler_section_config)


class HyperRAGBackendConfig(_StrictModel):
    """HyperRAG backend settings (engine knobs live in hyperrag.yml)."""

    config_path: Path
    repo_root: Path | None = None
    workspace_root: Path | None = None
    default_query_mode: Literal["hybrid", "vector", "bm25"] = "hybrid"
    default_top_k: int = Field(default=4, ge=1)
    runtime_concurrency: int = Field(default=64, ge=1, le=512)
    sync_bridge_concurrency: int = Field(default=64, ge=1, le=512)
    eager_start: bool = False
    eager_start_env: str | None = "VIKNOW_HYPERRAG_EAGER_START"

    @field_validator("eager_start_env")
    @classmethod
    def validate_eager_start_env(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not re.fullmatch(_ENV_VAR_NAME_PATTERN, cleaned):
            raise ValueError("must be a valid environment variable name")
        return cleaned

    def should_eager_start(self) -> bool:
        if self.eager_start:
            return True
        value = _resolved_env_value(self.eager_start_env)
        return value is not None and value.lower() in {"1", "true", "yes", "on"}


class FeishuKnowledgeConfig(_StrictModel):
    """Read-only Feishu Wiki discovery and synchronization settings.

    Credential values are resolved from environment variables at request time.
    Configuration files only store variable names so secrets never enter YAML,
    API responses, or logs.
    """

    enabled: bool = False
    enabled_env: str | None = "VIKNOW_FEISHU_ENABLED"
    api_base_url: str = "https://open.feishu.cn/open-apis"
    app_id_env: str = "FEISHU_APP_ID"
    app_secret_env: str = "FEISHU_APP_SECRET"
    space_id_env: str = "FEISHU_SPACE_ID"
    tenant_host_env: str = "FEISHU_TENANT_HOST"
    library_id_prefix: str = Field(
        default="feishu",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    page_size: int = Field(default=50, ge=1, le=50)
    sync_poll_interval_seconds: float = Field(default=5.0, ge=1, le=60)
    default_sync_interval_seconds: int = Field(default=10_800, ge=60, le=31_536_000)

    @field_validator(
        "enabled_env",
        "app_id_env",
        "app_secret_env",
        "space_id_env",
        "tenant_host_env",
    )
    @classmethod
    def validate_env_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not re.fullmatch(_ENV_VAR_NAME_PATTERN, cleaned):
            raise ValueError("must be a valid environment variable name")
        return cleaned

    def is_enabled(self) -> bool:
        if self.enabled:
            return True
        value = _resolved_env_value(self.enabled_env)
        return value is not None and value.lower() in {"1", "true", "yes", "on"}

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        cleaned = value.rstrip("/")
        try:
            parsed = urlsplit(cleaned)
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError as exc:
            raise ValueError("api_base_url must be a valid HTTPS URL") from exc
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("api_base_url must be a credential-free HTTPS URL")
        return cleaned

    def resolve_app_id(self) -> str | None:
        return _resolved_env_value(self.app_id_env)

    def resolve_app_secret(self) -> str | None:
        return _resolved_env_value(self.app_secret_env)

    def resolve_space_id(self) -> str | None:
        return _resolved_env_value(self.space_id_env)

    def resolve_tenant_host(self) -> str | None:
        value = _resolved_env_value(self.tenant_host_env)
        if value is None:
            return None
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        hostname = (parsed.hostname or "").lower()
        allowed_host = hostname == "feishu.cn" or hostname.endswith(".feishu.cn")
        allowed_host = (
            allowed_host or hostname == "larksuite.com" or hostname.endswith(".larksuite.com")
        )
        try:
            port = parsed.port
        except ValueError as exc:
            raise RuntimeError(
                f"{self.tenant_host_env} must contain a Feishu HTTPS hostname"
            ) from exc
        if (
            parsed.scheme != "https"
            or not allowed_host
            or parsed.username
            or parsed.password
            or port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"{self.tenant_host_env} must contain a Feishu HTTPS hostname")
        return hostname

    @property
    def required_env_names(self) -> tuple[str, str, str]:
        return (self.app_id_env, self.app_secret_env, self.space_id_env)

    def missing_env_names(self) -> list[str]:
        return [name for name in self.required_env_names if _resolved_env_value(name) is None]


class KnowledgeConfig(_StrictModel):
    """Knowledge backend settings."""

    engine: KnowledgeEngineType = KnowledgeEngineType.HYPERRAG
    default_library_id: str = Field(default="default", min_length=1)
    hyperrag: HyperRAGBackendConfig
    feishu: FeishuKnowledgeConfig = Field(default_factory=FeishuKnowledgeConfig)


class AppSettings(_StrictModel):
    """Application-level server settings."""

    name: str = "viknow"
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"] = "INFO"
    state_dir: Path
    # Single persistence root for session and knowledge files (JuiceFS mount in
    # multi-host deployments). ``None`` keeps per-component legacy paths.
    files_root: Path | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value


class ApiConfig(_StrictModel):
    """HTTP API settings."""

    host: str = "127.0.0.1"
    port: int = Field(default=5174, ge=1, le=65535)
    static_dir: Path
    request_timeout_seconds: float = Field(default=60, gt=0)


class _MonitoringHttpApiConfig(_StrictModel):
    """Shared connection settings for monitoring HTTP APIs."""

    base_url: str
    timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError as exc:
            raise ValueError("base_url must be a valid HTTP(S) URL") from exc
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("base_url must be a valid HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if any(character.isspace() for character in value):
            raise ValueError("base_url must not contain whitespace")
        return value


class PrometheusApiConfig(_MonitoringHttpApiConfig):
    """Prometheus HTTP API connection settings."""


class VllmApiConfig(_MonitoringHttpApiConfig):
    """vLLM HTTP API connection settings."""


class LiteLlmApiConfig(_MonitoringHttpApiConfig):
    """LiteLLM HTTP API connection settings."""

    api_key_env: str = Field(
        default="VIKNOW_LITELLM_API_KEY",
        pattern=_ENV_VAR_NAME_PATTERN,
    )
    timeout_seconds: float = Field(default=90.0, gt=0)


class NpmApiConfig(_MonitoringHttpApiConfig):
    """Nginx Proxy Manager HTTP API connection settings."""

    identity_env: str = Field(
        default="VIKNOW_NPM_IDENTITY",
        pattern=_ENV_VAR_NAME_PATTERN,
    )
    secret_env: str = Field(
        default="VIKNOW_NPM_SECRET",
        pattern=_ENV_VAR_NAME_PATTERN,
    )


class MonitoringApiConfig(_StrictModel):
    """External monitoring API settings exposed to ViKnow tools."""

    prometheus: PrometheusApiConfig | None = None
    vllm: VllmApiConfig | None = None
    litellm: LiteLlmApiConfig | None = None
    npm: NpmApiConfig | None = None


class LangfuseUsagePushConfig(_StrictModel):
    """Algorithm-side periodic usage push to the application callback.

    The worker only starts when ``enabled`` is true and ``callback_url`` is set.
    Each poll interval sends one batch of newly completed query runs and index
    jobs. Missing callback URL must not affect Q&A.
    """

    enabled: bool = True
    callback_url: str | None = None
    callback_token_env: str | None = "VIKNOW_LANGFUSE_USAGE_PUSH_TOKEN"
    poll_interval_seconds: float = Field(default=30.0, gt=0)
    timeout_seconds: float = Field(default=10.0, gt=0)
    # 0 means unlimited retries; >0 gives up after that many attempts with a log.
    max_attempts: int = Field(default=0, ge=0)


class LangfuseRunBindingConfig(_StrictModel):
    """Non-secret identity persisted with every newly created Langfuse trace link."""

    service_id: str | None = Field(default=None, min_length=1, max_length=128)
    environment: str | None = Field(default=None, min_length=1, max_length=64)
    source_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_complete_identity(self) -> LangfuseRunBindingConfig:
        values = (self.environment, self.source_id, self.project_id)
        if any(value is not None for value in values) and not all(values):
            raise ValueError("Langfuse run binding requires environment, source_id and project_id")
        return self

    @property
    def configured(self) -> bool:
        return self.source_id is not None


class LangfuseTracingConfig(_StrictModel):
    """Langfuse tracing settings for LangChain-compatible agent kernels."""

    enabled: bool = False
    enabled_env: str | None = "VIKNOW_LANGFUSE_ENABLED"
    # When null, index tracing follows the same enablement as agent tracing
    # (``enabled`` / ``VIKNOW_LANGFUSE_ENABLED``). Set true/false to override.
    index_enabled: bool | None = None
    usage_push: LangfuseUsagePushConfig = Field(default_factory=LangfuseUsagePushConfig)


class OtelTracingConfig(_StrictModel):
    """OpenTelemetry run-tree export (Jaeger / any OTLP HTTP collector)."""

    enabled: bool = False
    enabled_env: str | None = "VIKNOW_OTEL_ENABLED"
    service_name: str = "viknow"
    service_name_env: str | None = "VIKNOW_OTEL_SERVICE_NAME"
    exporter_endpoint: str | None = None
    exporter_endpoint_env: str | None = "VIKNOW_OTEL_EXPORTER_ENDPOINT"

    def is_enabled(self) -> bool:
        if self.enabled:
            return True
        value = _resolved_env_value(self.enabled_env)
        return value is not None and value.lower() in {"1", "true", "yes", "on"}

    def resolve_service_name(self) -> str:
        return _resolved_env_value(self.service_name_env) or self.service_name or "viknow"

    def resolve_exporter_endpoint(self) -> str | None:
        return _config_value(
            "exporter_endpoint",
            self.exporter_endpoint,
            self.exporter_endpoint_env,
        )


class ObservabilityConfig(_StrictModel):
    """Application observability integrations."""

    langfuse: LangfuseTracingConfig = Field(default_factory=LangfuseTracingConfig)
    otel: OtelTracingConfig = Field(default_factory=OtelTracingConfig)


class Mem0ComponentConfig(_StrictModel):
    """Single mem0 component configuration block.

    For ``vector_store``, ``provider`` may be omitted:
    ``agents.runtime.core.memory.config.resolve_mem0_config`` fills qdrant (sqlite) or
    pgvector (postgres) from ``persistence.backend``.
    Embedder/llm still require an explicit provider.
    """

    provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class Mem0Config(_StrictModel):
    """mem0 OSS configuration passed to Memory.from_config."""

    vector_store: Mem0ComponentConfig
    embedder: Mem0ComponentConfig | None = None
    llm: Mem0ComponentConfig | None = None


class MemoryConfig(_StrictModel):
    """mem0 infrastructure settings.

    Deployment capability only: whether a user gets memory is controlled per user
    via ``UserRecord.memory_enabled``, not by a process-level switch.
    """

    mem0: Mem0Config | None = None


class AgentModelOption(_StrictModel):
    """A user-selectable main Agent model."""

    ref: str | None = Field(default=None, min_length=1)
    id: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, min_length=1)
    provider_name: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    model_env: str | None = Field(default=None)
    base_url_env: str | None = Field(default=None)
    api_key_env: str | None = Field(default=None)
    max_input_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_selectable_identity(self) -> AgentModelOption:
        if self.ref is not None:
            return self
        if self.id is not None:
            return self
        if self.provider_name and self.model:
            return self
        raise ValueError("agent.model_options entries require id or provider_name and model")

    def selection_id(self) -> str:
        if self.ref:
            return normalized_model_ref(self.ref, required=False) or self.ref
        if self.id:
            return self.id
        if self.provider_name and self.model:
            return f"{self.provider_name}:{self.model}"
        raise RuntimeError("agent model option has no selectable id")

    def resolve_model(self) -> str | None:
        if self.ref:
            return model_name_from_ref(self.ref, required=False)
        return _config_value("model", self.model, self.model_env)


class AgentRuntimeConfig(_StrictModel):
    """Agent loop configuration."""

    kernel: AgentKernelType = AgentKernelType.DEEPAGENTS
    default_profile: str
    profile_dir: Path
    model: AgentModelConfig
    model_options: list[AgentModelOption] = Field(default_factory=list)
    skill_dirs: list[Path] = Field(default_factory=list)
    deepagents: DeepAgentsKernelConfig = Field(default_factory=DeepAgentsKernelConfig)
    hermes: HermesKernelConfig = Field(default_factory=HermesKernelConfig)

    def default_model_id(self) -> str | None:
        return self.model.selection_id()


class AppConfig(_StrictModel):
    """Top-level ViKnow server configuration."""

    app: AppSettings
    api: ApiConfig
    share: ShareConfig = Field(default_factory=ShareConfig)
    knowledge: KnowledgeConfig | None = None
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    learning: LearningProjectionConfig = Field(default_factory=LearningProjectionConfig)
    trace_console: TraceConsoleConfig = Field(default_factory=TraceConsoleConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    license: LicenseConfig = Field(default_factory=LicenseConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)
    self_learning_analysis: SelfLearningAnalysisConfig = Field(
        default_factory=SelfLearningAnalysisConfig
    )
    self_learning_oracle: SelfLearningOracleConfig = Field(default_factory=SelfLearningOracleConfig)
    self_learning_skill_optimization: SelfLearningSkillOptimizationConfig = Field(
        default_factory=SelfLearningSkillOptimizationConfig
    )
    vicoach: VicoachConfig = Field(default_factory=VicoachConfig)
    agent: AgentRuntimeConfig

    _config_path: Path | None = PrivateAttr(default=None)
    _profile_catalog: AgentProfileCatalog | None = PrivateAttr(default=None)

    @property
    def scheduler(self) -> SchedulerSectionConfig:
        """Alias for ``share.scheduler`` (ViKnow runtime + HyperRAG materialization)."""

        return self.share.scheduler

    @model_validator(mode="after")
    def validate_model_provider_refs(self) -> AppConfig:
        config = _apply_share_consumer_defaults(_merge_share_into_app_config(self))
        providers = config.share.model_providers
        _validate_config_model_ref(config.agent.model, providers, "agent.model")
        for index, option in enumerate(config.agent.model_options):
            _validate_option_model_ref(
                option,
                providers,
                f"agent.model_options[{index}]",
            )
        config = config.model_copy(
            update={
                "agent": config.agent,
                "memory": config.memory,
                "capabilities": config.capabilities.model_copy(
                    update={
                        "vicoach": config.capabilities.vicoach.model_copy(
                            update={
                                "chapters": _resolve_chapter_model_config(
                                    config.capabilities.vicoach.chapters, providers
                                )
                            }
                        )
                    }
                ),
                "eval": config.eval.model_copy(
                    update={
                        "inspect_tool": config.eval.inspect_tool.model_copy(
                            update={
                                "judge_model": resolve_tool_model_config(
                                    config.eval.inspect_tool.judge_model,
                                    providers,
                                    field_name="eval.inspect_tool.judge_model.ref",
                                )
                            }
                        ),
                        "skill_flow": config.eval.skill_flow.model_copy(
                            update={
                                "oracle_judge_model": resolve_tool_model_config(
                                    config.eval.skill_flow.oracle_judge_model,
                                    providers,
                                    field_name="eval.skill_flow.oracle_judge_model.ref",
                                ),
                                "analysis_model": resolve_tool_model_config(
                                    config.eval.skill_flow.analysis_model,
                                    providers,
                                    field_name="eval.skill_flow.analysis_model.ref",
                                ),
                            }
                        ),
                    }
                ),
                "self_learning_analysis": config.self_learning_analysis.model_copy(
                    update={
                        "model": resolve_tool_model_config(
                            config.self_learning_analysis.model,
                            providers,
                            field_name="self_learning_analysis.model.ref",
                        )
                    }
                ),
                "vicoach": config.vicoach.model_copy(
                    update={
                        "model": resolve_tool_model_config(
                            config.vicoach.model,
                            providers,
                            field_name="vicoach.model.ref",
                        )
                    }
                ),
            }
        )

        # Pydantic __init__ keeps self, so apply the validated normalization in place.
        for field_name in config.model_fields_set:
            setattr(self, field_name, getattr(config, field_name))
        return self

    @property
    def model_providers(self) -> dict[str, ModelProviderConfig]:
        """Alias for ``share.model_providers`` (agent/tools resolution)."""

        return self.share.model_providers

    @property
    def config_path(self) -> Path:
        if self._config_path is None:
            raise RuntimeError("AppConfig was not loaded from a file")
        return self._config_path

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    @property
    def tools_dir(self) -> Path:
        return self.config_dir / "tools"

    @property
    def profile_catalog(self) -> AgentProfileCatalog:
        if self._profile_catalog is None:
            raise RuntimeError("AppConfig has no loaded Agent Profile Catalog")
        return self._profile_catalog


def _mem0_openai_component_from_ref(
    ref: str,
    model_providers: dict[str, ModelProviderConfig],
    *,
    field_name: str,
) -> Mem0ComponentConfig:
    provider_name, model_token = _split_model_ref_literal(ref, field_name=field_name)
    provider = _require_model_provider(model_providers, provider_name, field_name)
    return Mem0ComponentConfig(
        provider="openai",
        config={
            "model": model_token,
            "api_key": f"${{{provider.api_key_env}}}",
            "openai_base_url": f"${{{provider.base_url_env}}}",
        },
    )


def _merge_share_into_app_config(config: AppConfig) -> AppConfig:
    """Copy share-owned connection/storage fields into app-facing config sections."""

    config = _merge_share_database_into_persistence(config)
    share = config.share
    share_updates: dict[str, object] = {}

    redis_url = _resolve_share_redis_url(share.redis)
    if redis_url:
        unified = share.scheduler.unified.model_copy(update={"redis_url": redis_url})
        share = share.model_copy(
            update={"scheduler": share.scheduler.model_copy(update={"unified": unified})}
        )
        share_updates["share"] = share

    app_updates: dict[str, object] = {}
    if share.files_root is not None:
        app_updates["app"] = config.app.model_copy(update={"files_root": share.files_root})

    if not share_updates and not app_updates:
        return config
    return config.model_copy(update={**share_updates, **app_updates})


def resolve_juicefs_meta_postgres(config: AppConfig) -> PostgresPersistenceConfig:
    """Resolve Postgres connection for JuiceFS metadata.

    ``share.database.juicefs_postgres`` / ``JUICEFS_POSTGRES_*`` override business
    ``persistence.postgres`` connection fields when set. Schema name always comes from
    ``persistence.postgres.juicefs_schema``.
    """

    business = config.persistence.postgres
    if business is None:
        raise RuntimeError("persistence.postgres is required to resolve JuiceFS meta postgres")

    juice_block = (
        config.share.database.juicefs_postgres if config.share.database is not None else None
    )

    def expand_juice(field_name: str, raw: str | None, *, env_name: str) -> str | None:
        if raw is not None:
            expanded = _expand_env_tokens(
                raw,
                field_name=f"share.database.juicefs_postgres.{field_name}",
                required=False,
            )
            if expanded and expanded.strip():
                return expanded.strip()
        env_value = os.getenv(env_name, "").strip()
        return env_value or None

    juice_host = expand_juice(
        "host", juice_block.host if juice_block else None, env_name="JUICEFS_POSTGRES_HOST"
    )
    juice_port_raw = expand_juice(
        "port", juice_block.port if juice_block else None, env_name="JUICEFS_POSTGRES_PORT"
    )
    juice_user = expand_juice(
        "user", juice_block.user if juice_block else None, env_name="JUICEFS_POSTGRES_USER"
    )
    juice_password = expand_juice(
        "password",
        juice_block.password if juice_block else None,
        env_name="JUICEFS_POSTGRES_PASSWORD",
    )
    juice_database = expand_juice(
        "database",
        juice_block.database if juice_block else None,
        env_name="JUICEFS_POSTGRES_DATABASE",
    )

    juice_port: int | None = None
    if juice_port_raw:
        juice_port = int(juice_port_raw)

    has_juice_override = any(
        value is not None
        for value in (juice_host, juice_port, juice_user, juice_password, juice_database)
    )
    if not has_juice_override:
        return business

    password = juice_password or business.password
    if password is None or not str(password).strip():
        password = os.getenv("POSTGRES_PASSWORD", "").strip() or None

    port = juice_port if juice_port is not None else business.port
    return PostgresPersistenceConfig(
        host=juice_host or business.host,
        port=port,
        user=juice_user or business.user,
        password=password,
        database=juice_database or business.database,
        juicefs_schema=business.juicefs_schema,
    )


def _merge_share_database_into_persistence(config: AppConfig) -> AppConfig:
    """Copy ``share.database.postgres`` connection fields into ``persistence.postgres``."""

    share_postgres = config.share.database.postgres
    if share_postgres is None:
        return config
    postgres = config.persistence.postgres or PostgresPersistenceConfig()
    updates: dict[str, object] = {}
    for field_name in ("host", "port", "user", "password", "database"):
        value = getattr(share_postgres, field_name, None)
        if value is not None:
            updates[field_name] = value
    if not updates:
        return config
    return config.model_copy(
        update={
            "persistence": config.persistence.model_copy(
                update={"postgres": postgres.model_copy(update=updates)}
            )
        }
    )


def _apply_share_consumer_defaults(config: AppConfig) -> AppConfig:
    """Fill agent/mem0 consumers from ``share.actio.models`` when yaml omits duplicate refs."""

    share = config.share
    llm_ref = _select_share_agent_model_ref(share)
    embed_ref = share.memory.embedder_ref or share.actio.models.embed
    mem_llm_ref = share.memory.llm_ref or share.actio.models.llm

    agent = config.agent
    if not agent.model.ref and not agent.model.model and not agent.model.model_env:
        agent = agent.model_copy(update={"model": agent.model.model_copy(update={"ref": llm_ref})})

    memory = config.memory
    if memory.mem0 is not None:
        mem0 = memory.mem0
        embedder = mem0.embedder or _mem0_openai_component_from_ref(
            embed_ref,
            share.model_providers,
            field_name="share.memory.embedder",
        )
        llm = mem0.llm or _mem0_openai_component_from_ref(
            mem_llm_ref,
            share.model_providers,
            field_name="share.memory.llm",
        )
        memory = memory.model_copy(
            update={"mem0": mem0.model_copy(update={"embedder": embedder, "llm": llm})}
        )

    return config.model_copy(update={"agent": agent, "memory": memory})


def _select_share_agent_model_ref(share: ShareConfig) -> str:
    """Choose the configured primary model unless its provider credential is absent."""

    primary_ref = share.agent.model_ref or share.actio.models.llm
    fallback_ref = share.agent.fallback_model_ref
    if fallback_ref is None:
        return primary_ref

    provider_name, _model = _split_model_ref_literal(
        primary_ref,
        field_name="share.agent.model_ref",
    )
    provider = _require_model_provider(
        share.model_providers,
        provider_name,
        "share.agent.model_ref",
    )
    if os.getenv(provider.api_key_env, "").strip():
        return primary_ref

    fallback_provider_name, _fallback_model = _split_model_ref_literal(
        fallback_ref,
        field_name="share.agent.fallback_model_ref",
    )
    _require_model_provider(
        share.model_providers,
        fallback_provider_name,
        "share.agent.fallback_model_ref",
    )
    return fallback_ref


def resolve_model_config(
    model_config: AgentModelConfig,
    model_providers: dict[str, ModelProviderConfig],
    *,
    field_name: str = "model",
) -> AgentModelConfig:
    """Resolve a model ref into an OpenAI-compatible model config."""

    ref = model_config.ref
    if ref:
        provider_name, model = _split_model_ref_literal(ref, field_name=field_name)
        provider_name, model, provider_config, gateway = _resolve_model_connection(
            model_providers,
            provider_name,
            model,
            field_name,
        )
        model_fields = _model_fields_from_ref_model(model, field_name=field_name)
        return model_config.model_copy(
            update={
                "ref": None,
                "provider": provider_config.provider,
                "provider_name": provider_name,
                **model_fields,
                "base_url": provider_config.base_url,
                "base_url_env": provider_config.base_url_env,
                "api_key_env": provider_config.api_key_env,
                "reasoning_content_roundtrip": provider_config.reasoning_content_roundtrip,
                "gateway": gateway,
            }
        )
    if model_config.provider_name:
        provider_config = _require_model_provider(
            model_providers,
            model_config.provider_name,
            field_name,
        )
        model = model_config.resolve_model()
        if model in provider_config.fallback_model_refs:
            return resolve_model_config(
                model_config.model_copy(update={"ref": f"{model_config.provider_name}:{model}"}),
                model_providers,
                field_name=field_name,
            )
        return model_config.model_copy(
            update={
                "provider": provider_config.provider,
                "base_url": provider_config.base_url,
                "base_url_env": provider_config.base_url_env,
                "api_key_env": provider_config.api_key_env,
                "reasoning_content_roundtrip": provider_config.reasoning_content_roundtrip,
            }
        )
    return model_config


def _resolve_chapter_model_config(chapters, model_providers):
    if chapters is None or not chapters.ref:
        return chapters
    resolved = resolve_tool_model_config(
        ToolModelConfig(ref=chapters.ref, api_key_env="OPENAI_API_KEY"),
        model_providers,
        field_name="capabilities.vicoach.chapters.ref",
    )
    if resolved.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError("chapter model provider must be openai-compatible")
    return type(chapters).model_validate(
        {
            **chapters.model_dump(),
            "ref": None,
            "model": resolved.model,
            "model_env": resolved.model_env,
            "base_url": resolved.base_url,
            "base_url_env": resolved.base_url_env,
            "api_key_env": resolved.api_key_env,
        }
    )


def resolve_tool_model_config[T: ToolModelConfig](
    model_config: T,
    model_providers: dict[str, ModelProviderConfig],
    *,
    field_name: str,
) -> T:
    """Resolve a ViKnow-owned tool model ref while preserving tool settings."""

    ref = model_config.ref
    if not ref:
        return model_config
    provider_name, model = _split_model_ref_literal(ref, field_name=field_name)
    _provider_name, model, provider_config, gateway = _resolve_model_connection(
        model_providers, provider_name, model, field_name
    )
    model_fields = _model_fields_from_ref_model(model, field_name=field_name)
    return model_config.model_copy(
        update={
            "ref": None,
            "provider": provider_config.provider,
            **model_fields,
            "base_url": provider_config.base_url,
            "base_url_env": provider_config.base_url_env,
            "api_key_env": provider_config.api_key_env,
            "gateway": gateway,
        }
    )


def _resolve_model_connection(
    model_providers: dict[str, ModelProviderConfig],
    provider_name: str,
    model: str,
    field_name: str,
) -> tuple[str, str, ModelProviderConfig, ModelGatewayRoute | None]:
    """Resolve one explicit gateway alias to its independent direct connection."""

    provider = _require_model_provider(model_providers, provider_name, field_name)
    fallback_ref = provider.fallback_model_refs.get(model)
    if fallback_ref is None:
        return provider_name, model, provider, None
    fallback_name, fallback_model = _split_model_ref_literal(fallback_ref, field_name=field_name)
    fallback_provider = _require_model_provider(model_providers, fallback_name, field_name)
    if fallback_model in fallback_provider.fallback_model_refs:
        raise ValueError(f"{field_name} fallback_model_refs must reference a direct model")
    gateway = ModelGatewayRoute(
        model=model,
        base_url=provider.base_url or "",
        base_url_env=provider.base_url_env or "",
        api_key_env=provider.api_key_env,
        thinking_protocol="none",
        max_recovery_attempts=provider.gateway_max_recovery_attempts,
        recovery_backoff_seconds=provider.gateway_recovery_backoff_seconds,
        require_gateway=provider.gateway_require_gateway,
        capacity_recovery_enabled=provider.gateway_capacity_recovery_enabled,
    )
    return fallback_name, fallback_model, fallback_provider, gateway


def resolve_tool_model_config_from_actio[T: ToolModelConfig | ASRToolConfig](
    model_config: T,
    config: AppConfig,
    model_providers: dict[str, ModelProviderConfig],
    actio_config: ActioIntegrationConfig | None,
    *,
    field_name: str,
) -> T:
    """Resolve tool refs, allowing actio:<section> to reuse Actio model config."""

    ref = model_config.ref
    if not ref:
        return model_config
    provider_name, section_name = _split_model_ref_literal(ref, field_name=field_name)
    if provider_name != ACTIO_MODEL_REF_PROVIDER:
        return resolve_tool_model_config(model_config, model_providers, field_name=field_name)
    if actio_config is None:
        raise ValueError(f"{field_name} references actio config, but actio.yaml is missing")
    if section_name == "asr":
        toolkit_asr = _selected_actio_toolkit_asr_model_section(actio_config, selector="qa")
        if toolkit_asr is not None:
            toolkit_asr = _expand_env_tokens_in_object(
                toolkit_asr,
                field_name=f"{field_name}.actio.asr",
                required=True,
            )
            if not isinstance(toolkit_asr, dict):
                raise ValueError("actio.toolkit.asr selected strategy is invalid")
            toolkit_asr = _apply_actio_gateway_provider_defaults(toolkit_asr, config)
            resolved = _resolve_actio_tool_model_section(
                toolkit_asr,
                model_providers,
                field_name=f"{field_name}.actio.asr",
            )
            model_fields = _model_fields_from_ref_model(resolved["model"], field_name=field_name)
            provider = (
                ModelProvider(str(toolkit_asr.get("provider")))
                if isinstance(toolkit_asr, dict)
                and toolkit_asr.get("provider") in {"granite", "ark"}
                else ModelProvider.OPENAI_COMPATIBLE
            )
            return model_config.model_copy(
                update={
                    "ref": None,
                    "provider": provider,
                    **model_fields,
                    "base_url": resolved["base_url"],
                    "base_url_env": resolved.get("base_url_env"),
                    "api_key_env": resolved["api_key_env"],
                    **_actio_asr_runtime_fields(toolkit_asr),
                }
            )
    if section_name == "ocr":
        if actio_config.toolkit is not None:
            ocr = actio_config.toolkit.get("ocr")
            if not isinstance(ocr, dict):
                raise ValueError("actio.toolkit.ocr is required for actio:ocr")
            selected = ocr.get("selected")
            strategy = ocr.get(selected)
            if selected == "vlm" and (
                not isinstance(strategy, dict)
                or (not strategy.get("ref") and not strategy.get("model_name"))
            ):
                resolved = _resolve_actio_tool_model_section(
                    config.share.actio.models.vlm,
                    model_providers,
                    field_name=f"{field_name}.actio.ocr",
                )
                model_fields = _model_fields_from_ref_model(
                    resolved["model"],
                    field_name=field_name,
                )
                return model_config.model_copy(
                    update={
                        "ref": None,
                        "provider": ModelProvider.OPENAI_COMPATIBLE,
                        **model_fields,
                        "base_url": resolved["base_url"],
                        "base_url_env": resolved.get("base_url_env"),
                        "api_key_env": resolved["api_key_env"],
                    }
                )
            if not isinstance(strategy, dict):
                raise ValueError("actio.toolkit.ocr selected strategy is invalid")
            strategy = _expand_env_tokens_in_object(
                strategy,
                field_name=f"{field_name}.actio.ocr",
                required=True,
            )
            if not isinstance(strategy, dict):
                raise ValueError("actio.toolkit.ocr selected strategy is invalid")
            configured = model_config.model_copy(update=strategy)
            return resolve_tool_model_config(configured, model_providers, field_name=field_name)
        if actio_config.ocr_config is None:
            raise ValueError("actio.yaml must set ocr_config for actio:ocr")
        strategy_path = _resolve_path(actio_config.ocr_config, config.config_dir)
        configured = model_config.model_copy(update=_load_yaml_mapping(strategy_path))
        return resolve_tool_model_config(configured, model_providers, field_name=field_name)
    section = _load_actio_model_call_refs(config, actio_config).get(section_name)
    if section is None:
        raise ValueError(f"{field_name} references missing actio model section: {section_name}")
    resolved = _resolve_actio_tool_model_section(
        section,
        model_providers,
        field_name=f"{field_name}.actio.{section_name}",
    )
    model_fields = _model_fields_from_ref_model(resolved["model"], field_name=field_name)
    return model_config.model_copy(
        update={
            "ref": None,
            "provider": ModelProvider.OPENAI_COMPATIBLE,
            **model_fields,
            "base_url": resolved["base_url"],
            "base_url_env": resolved.get("base_url_env"),
            "api_key_env": resolved["api_key_env"],
        }
    )


def resolve_locate_anything_config_from_actio(
    locate_config: LocateAnythingToolConfig,
    actio_config: ActioIntegrationConfig | None,
    *,
    config: AppConfig,
    field_name: str,
) -> LocateAnythingToolConfig:
    """Resolve LocateAnything native service refs from actio.yaml toolkit config."""

    ref = locate_config.ref
    if not ref:
        return locate_config
    provider_name, section_name = _split_model_ref_literal(ref, field_name=field_name)
    if provider_name != ACTIO_MODEL_REF_PROVIDER:
        raise ValueError(f"{field_name} only supports actio:<section> refs")
    if section_name != "locate_anything":
        raise ValueError(f"{field_name} references unsupported actio section: {section_name}")
    if actio_config is None:
        raise ValueError(f"{field_name} references actio config, but actio.yaml is missing")
    if actio_config.toolkit is None:
        raise ValueError("actio.toolkit.locate_anything is required for actio:locate_anything")
    toolkit = actio_config.toolkit.get("locate_anything")
    if not isinstance(toolkit, dict):
        raise ValueError("actio.toolkit.locate_anything is required for actio:locate_anything")
    selected = toolkit.get("selected")
    strategy: object = toolkit
    if isinstance(selected, str) and selected:
        strategy = toolkit.get(selected)
    if not isinstance(strategy, dict):
        raise ValueError("actio.toolkit.locate_anything selected strategy is invalid")
    # base_url 等字段可能含 ${ENV} 占位符（如 ${ACTIO_SERVER_URL}/locate），必须先展开
    # （OCR 路径 1268 行已有 expand；locate 路径此前漏了，导致字面 ${ACTIO_SERVER_URL} 进 base_url）
    strategy = _expand_env_tokens_in_object(
        strategy,
        field_name=f"{field_name}.actio.locate_anything",
        required=True,
    )
    allowed = {"base_url", "base_url_env", "api_key_env", "timeout_seconds"}
    update = {key: value for key, value in strategy.items() if key in allowed}
    if "api_key_env" not in update and "api_key" not in update:
        private = config.model_providers.get("private")
        if private is not None:
            update["api_key_env"] = private.api_key_env
    return locate_config.model_copy(update={"ref": None, **update})


def normalized_model_ref(ref: str, *, required: bool) -> str | None:
    provider_name, model = _split_model_ref(ref, field_name="model ref", required=required)
    if provider_name is None or model is None:
        return None
    return f"{provider_name}:{model}"


def model_name_from_ref(ref: str, *, required: bool) -> str | None:
    _provider_name, model = _split_model_ref(ref, field_name="model ref", required=required)
    return model


def default_app_config_path(workspace_root: Path | None = None) -> Path:
    override = os.getenv("VIKNOW_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    root = workspace_root or Path.cwd()
    return (root / "config" / "local" / "app.yaml").resolve()


def load_app_config(path: Path | None = None, *, workspace_root: Path | None = None) -> AppConfig:
    from agents.runtime.core.env import load_deploy_env

    config_path = (path or default_app_config_path(workspace_root)).expanduser().resolve()
    load_deploy_env(config_path)
    raw = _expand_persistence_backend_tokens(_load_yaml_mapping(config_path))
    config = AppConfig.model_validate(raw)
    config._config_path = config_path
    config = _resolve_app_config_paths(config)
    config._profile_catalog = load_profile_catalog(
        config.agent.profile_dir,
        default_profile_id=config.agent.default_profile,
    )
    return config


def load_deepagents_kernel_config(path: Path) -> DeepAgentsKernelConfig:
    """Load a DeepAgents kernel config from YAML.

    The loader is intentionally small: schema validation lives in the Pydantic
    models, and secrets remain indirect through ``api_key_env``.
    """

    return DeepAgentsKernelConfig.model_validate(_load_yaml_mapping(path))


def load_tool_configs(config: AppConfig) -> ToolConfigs:
    """Parse optional environment Tool configs without runtime filesystem writes."""

    loaded_actio = _load_actio_integration_config(config)
    knowledge = _load_knowledge_tool_config_if_present(config)
    multimodal = _load_multimodal_tool_config_if_present(config, loaded_actio)
    monitoring = _load_optional_model(
        config.tools_dir / "monitoring.yaml",
        MonitoringApiConfig,
        expand_env=True,
        expand_required=True,
    )
    return ToolConfigs(
        knowledge=knowledge,
        multimodal=multimodal,
        actio=None,
        monitoring=monitoring,
    )


def materialize_runtime_tool_configs(
    config: AppConfig,
    parsed_tool_configs: ToolConfigs,
) -> ToolConfigs:
    """Return a runtime-ready copy, materializing Actio config when configured."""

    loaded_actio = _load_actio_integration_config(config)
    actio = None
    if loaded_actio is not None:
        models_config_path = materialize_actio_models_config(config, loaded_actio)
        if models_config_path is not None:
            actio = ActioToolConfig(models_config_path=models_config_path)
    return parsed_tool_configs.model_copy(update={"actio": actio})


def _load_optional_model[T: BaseModel](
    path: Path,
    model_type: type[T],
    *,
    expand_env: bool = False,
    expand_required: bool = False,
) -> T | None:
    if not path.exists():
        return None
    raw: object = _load_yaml_mapping(path)
    if expand_env:
        raw = _expand_env_tokens_in_object(
            raw,
            field_name=str(path),
            required=expand_required,
        )
    if not isinstance(raw, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    return model_type.model_validate(raw)


def _load_knowledge_tool_config_if_present(config: AppConfig) -> KnowledgeToolConfig | None:
    loaded = _load_optional_model(config.tools_dir / "knowledge.yaml", KnowledgeToolConfig)
    if loaded is None:
        return None
    return loaded.model_copy(
        update={
            "index_dir": _resolve_path(loaded.index_dir, config.config_dir),
            "spec_path": _resolve_path(loaded.spec_path, config.config_dir),
        }
    )


def _load_multimodal_tool_config_if_present(
    config: AppConfig,
    actio_config: ActioIntegrationConfig | None,
) -> MultimodalToolConfig | None:
    loaded = _load_optional_model(config.tools_dir / "multimodal.yaml", MultimodalToolConfig)
    if loaded is None:
        return None
    return _resolve_multimodal_tool_configs(loaded, config, actio_config)


def materialize_actio_models_config(
    config: AppConfig,
    actio_config: ActioIntegrationConfig | None = None,
) -> Path | None:
    """Render ViKnow actio model refs into actio's native model-call YAML."""

    if actio_config is None:
        actio_config = _load_actio_integration_config(config)
        if actio_config is None:
            return None
    raw = _actio_model_call_refs_for_materialization(config, actio_config)
    rendered: dict[str, dict[str, object]] = {}
    for section_name in ("vlm", "llm", "embed", "asr", "ocr"):
        section = raw.get(section_name)
        if section is None:
            continue
        rendered[section_name] = _render_actio_model_section(
            section,
            config.model_providers,
            field_name=f"actio.{section_name}",
            passthrough_keys=(
                _ACTIO_ASR_PASSTHROUGH_KEYS
                if section_name == "asr"
                else ("timeout_seconds",)
                if section_name == "vlm"
                else ()
            ),
        )
    target_path = config.app.state_dir / "actio" / "model_calls.yaml"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml_mapping(target_path, rendered)
    return target_path


def _bool_config_value(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return default


def materialize_hyperrag_config(config: AppConfig) -> Path:
    """Render HyperRAG config with ViKnow-owned integration config injected."""

    if config.knowledge is None:
        raise RuntimeError("knowledge backend config is required")

    source_path = config.knowledge.hyperrag.config_path
    raw = dict(_load_yaml_mapping(source_path))
    raw = _flatten_hyperrag_nested_config(raw)
    actio_config = _load_actio_integration_config(config)
    if actio_config is not None:
        models_config_path = materialize_actio_models_config(config, actio_config)
        existing_actio = raw.get("actio") if isinstance(raw.get("actio"), dict) else {}
        actio_payload: dict[str, Any] = {
            str(key): str(value) for key, value in existing_actio.items()
        }
        if models_config_path is not None:
            actio_payload["models_config_path"] = str(models_config_path)
        if actio_config.toolkit is not None:
            toolkit_dir = config.app.state_dir / "actio" / "toolkit"
            segmentation = actio_config.toolkit.get("segmentation") or {}
            selected_segment = (
                segmentation.get("selected") if isinstance(segmentation, dict) else None
            )
            segment_payload = (
                segmentation.get(selected_segment) if isinstance(segmentation, dict) else None
            )
            caption = actio_config.toolkit.get("caption") or {}
            if not isinstance(segment_payload, dict) or not isinstance(caption, dict):
                raise ValueError(
                    "actio.toolkit requires selected segmentation and caption sections"
                )
            segment_path = toolkit_dir / "segmentation.yml"
            aggregate_path = toolkit_dir / "aggregate.yml"
            merge_path = toolkit_dir / "caption_merge.yml"
            merge_dtw_path = toolkit_dir / "sop_alignment_dtw.yml"
            env_values = _load_repo_env_values(config)
            _write_yaml_mapping(
                segment_path,
                _expand_env_tokens_in_object(
                    segment_payload,
                    field_name="actio.toolkit.segmentation",
                    required=True,
                    env_values=env_values,
                ),
            )
            _write_yaml_mapping(
                aggregate_path,
                _expand_env_tokens_in_object(
                    dict(caption.get("aggregate") or {}),
                    field_name="actio.toolkit.caption.aggregate",
                    required=True,
                    env_values=env_values,
                ),
            )
            _write_yaml_mapping(
                merge_path,
                {
                    "merge": _expand_env_tokens_in_object(
                        dict((caption.get("sop_alignment") or {}).get("llm") or {}),
                        field_name="actio.toolkit.caption.sop_alignment.llm",
                        required=True,
                        env_values=env_values,
                    )
                },
            )
            _write_yaml_mapping(
                merge_dtw_path,
                {
                    "merge": _expand_env_tokens_in_object(
                        dict((caption.get("sop_alignment") or {}).get("dtw") or {}),
                        field_name="actio.toolkit.caption.sop_alignment.dtw",
                        required=True,
                        env_values=env_values,
                    )
                },
            )
            asr_caption = caption.get("asr") if isinstance(caption.get("asr"), dict) else {}
            vlm_caption = caption.get("vlm") if isinstance(caption.get("vlm"), dict) else {}
            if "max_concurrency" in asr_caption:
                actio_payload["asr_max_concurrency"] = max(1, int(asr_caption["max_concurrency"]))
            if "keep_segment_outputs" in asr_caption:
                actio_payload["keep_asr_segment_outputs"] = _bool_config_value(
                    asr_caption["keep_segment_outputs"]
                )
            actio_payload["keep_asr_audio_outputs"] = _bool_config_value(
                asr_caption.get("keep_audio_outputs"), default=True
            )
            if "max_concurrency" in vlm_caption:
                actio_payload["vlm_max_concurrency"] = max(1, int(vlm_caption["max_concurrency"]))
            actio_payload.update(
                {
                    "segment_config": str(segment_path),
                    "aggregate_config": str(aggregate_path),
                    "merge_config": str(merge_path),
                    "merge_dtw_config": str(merge_dtw_path),
                }
            )
        for field_name in (
            "aggregate_config",
            "merge_config",
            "merge_dtw_config",
            "sop_schema_path",
        ):
            value = getattr(actio_config, field_name)
            if value is not None:
                actio_payload[field_name] = str(_resolve_path(value, config.config_dir))
        if actio_config.segmentation_config is not None:
            actio_payload["segment_config"] = str(
                _resolve_path(actio_config.segmentation_config, config.config_dir)
            )
        if actio_config.device is not None:
            actio_payload["device"] = actio_config.device
        for path_key in (
            "models_config_path",
            "segment_config",
            "aggregate_config",
            "merge_config",
            "merge_dtw_config",
            "sop_schema_path",
        ):
            if path_key in actio_payload:
                actio_payload[path_key] = str(
                    _resolve_path(Path(actio_payload[path_key]), source_path.parent)
                )
        raw["actio"] = actio_payload
    raw["actio_config_dir"] = str(_resolve_default_actio_config_dir(config))
    _inject_hyperrag_from_share(raw, config)
    _expand_hyperrag_runtime_env_refs(raw)

    if "application_config_path" in raw:
        raw["application_config_path"] = str(
            _resolve_path(Path(str(raw["application_config_path"])), source_path.parent)
        )
    raw["working_dir"] = str(resolve_knowledge_libraries_root(resolve_files_root(config)))
    _expand_hyperrag_env(raw)
    _expand_hyperrag_private_placeholders(raw)

    target_path = config.app.state_dir / "hyperrag" / "hyperrag.yml"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml_mapping(target_path, raw)
    return target_path


def _inject_hyperrag_from_share(raw: dict[str, object], config: AppConfig) -> None:
    """Inject all HyperRAG fields owned by ``app.yaml`` → ``share`` (plus actio.yaml toolkit)."""

    if config.knowledge is None:
        return
    _inject_hyperrag_asr_config(raw, config)
    _inherit_hyperrag_models_from_share(raw, config)
    _inherit_hyperrag_rerank_endpoints_from_share(raw, config)
    _inherit_hyperrag_shared_runtime_env_from_share(raw, config)
    unified = config.share.scheduler.unified
    raw["scheduler"] = {"unified": unified.model_dump(mode="json")}
    raw["max_concurrent_job_tasks"] = unified.max_concurrent_job_tasks
    raw["redis_uri"] = _resolve_share_redis_url(config.share.redis) or unified.redis_url
    env = raw.get("env")
    if not isinstance(env, dict):
        env = {}
        raw["env"] = env
    postgres = config.share.database.postgres
    if postgres is not None:
        inherited = {
            "POSTGRES_HOST": postgres.host,
            "POSTGRES_PORT": None if postgres.port is None else str(postgres.port),
            "POSTGRES_USER": postgres.user,
            "POSTGRES_PASSWORD": postgres.password,
            "POSTGRES_DATABASE": postgres.database,
        }
        for key, value in inherited.items():
            if value is None:
                continue
            text = str(value).strip()
            if text:
                env[key] = text
    neo4j = config.share.neo4j
    for env_key, value in (
        ("NEO4J_URI", neo4j.uri),
        ("NEO4J_USERNAME", neo4j.username),
        ("NEO4J_PASSWORD", neo4j.password),
    ):
        text = str(value).strip()
        if text:
            env[env_key] = text


def _flatten_hyperrag_nested_config(raw: dict) -> dict:
    """Flatten ViKnow-friendly nested HyperRAG sections into HyperRAG keys."""

    out = dict(raw)
    models = out.pop("models", {})
    if not isinstance(models, dict):
        models = {}

    llm = models.get("llm") if isinstance(models.get("llm"), dict) else {}
    embedding = models.get("embedding") if isinstance(models.get("embedding"), dict) else {}
    video_embedding = (
        models.get("video_embedding") if isinstance(models.get("video_embedding"), dict) else {}
    )
    whisper = models.get("whisper") if isinstance(models.get("whisper"), dict) else {}

    _set_default(out, "llm_model", llm.get("model"))
    _set_default(out, "llm_base_url", llm.get("base_url"))
    _set_default(out, "llm_api_key", llm.get("api_key"))
    _set_default(out, "llm_model_kwargs", llm.get("model_kwargs"))
    _set_default(out, "llm_timeout", llm.get("timeout") or llm.get("timeout_seconds"))

    _set_default(out, "embedding_model", embedding.get("model"))
    _set_default(out, "embedding_provider", embedding.get("provider"))
    _set_default(out, "embedding_base_url", embedding.get("base_url"))
    _set_default(out, "embedding_api_key", embedding.get("api_key"))
    _set_default(out, "embedding_dim", embedding.get("dim") or embedding.get("embedding_dim"))
    _set_default(out, "embedding_batch_num", embedding.get("batch_num"))
    _set_default(out, "embedding_func_max_async", embedding.get("max_async"))
    _set_default(
        out,
        "embedding_timeout",
        embedding.get("timeout") or embedding.get("timeout_seconds"),
    )

    _set_default(out, "enable_video_embedding", video_embedding.get("enabled"))
    _set_default(out, "video_embedding_model", video_embedding.get("model"))
    _set_default(out, "video_embedding_provider", video_embedding.get("provider"))
    _set_default(out, "video_embedding_base_url", video_embedding.get("base_url"))
    _set_default(out, "video_embedding_api_key", video_embedding.get("api_key"))
    _set_default(
        out,
        "video_embedding_dim",
        video_embedding.get("dim") or video_embedding.get("embedding_dim"),
    )
    _set_default(out, "video_embedding_params", video_embedding.get("params"))
    _set_default(out, "video_embedding_batch_num", video_embedding.get("batch_num"))

    _set_default(out, "whisper_base_url", whisper.get("base_url"))
    _set_default(out, "whisper_api_key", whisper.get("api_key"))

    rerank = out.pop("rerank", {})
    if not isinstance(rerank, dict):
        rerank = {}
    _set_default(out, "rerank_enabled", rerank.get("enabled"))
    _set_default(out, "rerank_base_url", rerank.get("base_url"))
    _set_default(out, "rerank_model", rerank.get("model"))
    _set_default(out, "rerank_api_key", rerank.get("api_key"))
    _set_default(out, "rerank_mode", rerank.get("mode"))
    _set_default(out, "rerank_top_k", rerank.get("top_k"))
    _set_default(out, "rerank_score_threshold", rerank.get("score_threshold"))
    _set_default(out, "rerank_batch_size", rerank.get("batch_size"))
    _set_default(out, "rerank_batch_concurrency", rerank.get("batch_concurrency"))
    _set_default(out, "rerank_timeout_seconds", rerank.get("timeout_seconds"))
    _set_default(out, "rerank_max_retries", rerank.get("max_retries"))
    _set_default(out, "rerank_max_inline_media_mb", rerank.get("max_inline_media_mb"))

    return out


def _set_default(data: dict, key: str, value: object) -> None:
    if value is not None and key not in data:
        data[key] = value


def _inject_hyperrag_asr_config(raw: dict[str, object], config: AppConfig) -> None:
    provider_config = config.model_providers.get("asr")
    if provider_config is None:
        return
    _set_default(
        raw,
        "whisper_base_url",
        _runtime_value_from_provider(
            value=provider_config.base_url,
            env_name=provider_config.base_url_env,
        ),
    )
    _set_default(
        raw,
        "whisper_api_key",
        _runtime_value_from_provider(value=None, env_name=provider_config.api_key_env),
    )


def _inherit_hyperrag_models_from_share(raw: dict[str, object], config: AppConfig) -> None:
    """Wire HyperRAG LLM / embedding from ``share.model_providers``."""

    private = config.model_providers.get("private")
    if private is not None:
        raw["llm_model"] = _deploy_env_ref("VIKNOW_PRIVATE_MODEL")
        llm_base_url = _runtime_value_from_provider(
            value=private.base_url,
            env_name=private.base_url_env,
        )
        llm_api_key = _runtime_value_from_provider(
            value=None,
            env_name=private.api_key_env,
        )
        if llm_base_url:
            raw["llm_base_url"] = llm_base_url
        if llm_api_key:
            raw["llm_api_key"] = llm_api_key

    embedding = config.model_providers.get("embedding")
    if embedding is not None:
        raw["embedding_model"] = _deploy_env_ref("VIKNOW_EMBEDDING_MODEL")
        raw["embedding_provider"] = raw.get("embedding_provider") or "vllm"
        embedding_base_url = _runtime_value_from_provider(
            value=embedding.base_url,
            env_name=embedding.base_url_env,
        )
        embedding_api_key = _runtime_value_from_provider(
            value=None,
            env_name=embedding.api_key_env,
        )
        if embedding_base_url:
            raw["embedding_base_url"] = embedding_base_url
        if embedding_api_key:
            raw["embedding_api_key"] = embedding_api_key

    if _bool_config_value(raw.get("enable_video_embedding"), default=True):
        raw["enable_video_embedding"] = True
        raw["video_embedding_provider"] = raw.get("video_embedding_provider") or raw.get(
            "embedding_provider"
        )
        for target_key, source_key in (
            ("video_embedding_model", "embedding_model"),
            ("video_embedding_base_url", "embedding_base_url"),
            ("video_embedding_api_key", "embedding_api_key"),
        ):
            source_value = raw.get(source_key)
            if source_value is not None:
                raw[target_key] = source_value


def _inherit_hyperrag_rerank_endpoints_from_share(
    raw: dict[str, object],
    config: AppConfig,
) -> None:
    """Wire HyperRAG rerank endpoints from ``share.model_providers.rerank``."""

    if config.knowledge is None:
        return
    provider = config.model_providers.get("rerank")
    if provider is None:
        return
    raw["rerank_model"] = _deploy_env_ref("HYPERRAG_RERANK_MODEL")
    rerank_base_url = _runtime_value_from_provider(
        value=provider.base_url,
        env_name=provider.base_url_env,
    )
    rerank_api_key = _runtime_value_from_provider(
        value=None,
        env_name=provider.api_key_env,
    )
    if rerank_base_url:
        raw["rerank_base_url"] = rerank_base_url
    if rerank_api_key:
        raw["rerank_api_key"] = rerank_api_key


def _inherit_hyperrag_shared_runtime_env_from_share(
    raw: dict[str, object],
    config: AppConfig,
) -> None:
    """Inject MinerU + Langfuse env owned by ``share`` (not hyperrag.yml)."""

    if config.knowledge is None:
        return
    inherited_env: dict[str, str] = {
        "LANGFUSE_HOST": config.share.langfuse.host,
        "LANGFUSE_SECRET_KEY": config.share.langfuse.secret_key,
        "LANGFUSE_PUBLIC_KEY": config.share.langfuse.public_key,
        "LANGFUSE_MEDIA_UPLOAD_ENABLED": config.share.langfuse.media_upload_enabled,
        "LANGFUSE_TRACING_ENABLED": config.share.langfuse.tracing_enabled,
    }
    private = config.model_providers.get("private")
    if private is not None:
        mineru_server = _runtime_value_from_provider(
            value=private.base_url,
            env_name=private.base_url_env,
        )
        mineru_api_key = _runtime_value_from_provider(
            value=None,
            env_name=private.api_key_env,
        )
        if mineru_server:
            inherited_env["MINERU_VL_SERVER"] = mineru_server
        if mineru_api_key:
            inherited_env["MINERU_VL_API_KEY"] = mineru_api_key
    inherited_env["MINERU_VL_MODEL_NAME"] = _deploy_env_ref("HYPERRAG_MINERU_VL_MODEL_NAME")
    inherited_env["MINERU_PROCESSING_WINDOW_SIZE"] = _deploy_env_ref(
        "HYPERRAG_MINERU_PROCESSING_WINDOW_SIZE"
    )

    env = raw.get("env")
    if not isinstance(env, dict):
        env = {}
    env.update(inherited_env)
    raw["env"] = env


def _deploy_env_ref(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    return f"${{{name}}}"


def _expand_hyperrag_env(raw: dict[str, object]) -> None:
    env = raw.get("env")
    if not isinstance(env, dict):
        return
    expanded: dict[str, object] = {}
    for key, value in env.items():
        if isinstance(value, str):
            resolved = _expand_env_tokens(
                value,
                field_name=f"hyperrag.env.{key}",
                required=False,
            )
            if resolved is None or not resolved.strip():
                # Drop unset optional placeholders (private topology vars).
                continue
            expanded[str(key)] = resolved
        else:
            expanded[str(key)] = value
    raw["env"] = expanded


def _expand_hyperrag_private_placeholders(raw: dict[str, object]) -> None:
    """Expand optional ``${ENV}`` placeholders in HyperRAG runtime config keys."""

    def expand_preserving(value: object, *, field_name: str) -> object:
        if isinstance(value, str) and _ENV_TOKEN_RE.search(value):
            expanded = _expand_env_tokens(value, field_name=field_name, required=False)
            return value if expanded is None else expanded
        if isinstance(value, dict):
            return {
                key: expand_preserving(item, field_name=f"{field_name}.{key}")
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                expand_preserving(item, field_name=f"{field_name}[{index}]")
                for index, item in enumerate(value)
            ]
        return value

    # ``materialize_hyperrag_config`` flattens nested model/rerank sections into
    # top-level HyperRAG runtime keys before this expansion step.
    for field_name in (
        "redis_uri",
        "llm_model",
        "llm_base_url",
        "embedding_model",
        "embedding_base_url",
        "video_embedding_model",
        "video_embedding_base_url",
        "whisper_base_url",
        "rerank_model",
        "rerank_base_url",
    ):
        value = raw.get(field_name)
        if isinstance(value, str):
            raw[field_name] = expand_preserving(value, field_name=f"hyperrag.{field_name}")

    # Keep support for legacy nested sections when they appear in source config.
    for section_name in ("models", "rerank"):
        section = raw.get(section_name)
        if isinstance(section, dict):
            raw[section_name] = expand_preserving(section, field_name=f"hyperrag.{section_name}")

    # 统一调度段（scheduler.unified）也支持 ${ENV} 展开，与 redis_uri 同级。
    # private 部署通过环境变量注入 scheduler 的 Redis 连接串。
    scheduler_section = raw.get("scheduler")
    if isinstance(scheduler_section, dict):
        raw["scheduler"] = expand_preserving(scheduler_section, field_name="hyperrag.scheduler")


def _expand_hyperrag_runtime_env_refs(raw: dict[str, object]) -> None:
    for key in (
        "llm_api_key",
        "embedding_api_key",
        "video_embedding_api_key",
        "rerank_api_key",
    ):
        value = raw.get(key)
        if isinstance(value, str):
            raw[key] = _expand_env_tokens(value, field_name=f"hyperrag.{key}", required=True)


def _load_actio_integration_config(config: AppConfig) -> ActioIntegrationConfig | None:
    actio_config_path = config.config_dir / "actio.yaml"
    if actio_config_path.exists():
        return ActioIntegrationConfig.model_validate(_load_yaml_mapping(actio_config_path))

    legacy_config_path = config.config_dir / "actio" / "integration.yaml"
    if not legacy_config_path.exists():
        return None
    return ActioIntegrationConfig.model_validate(_load_yaml_mapping(legacy_config_path))


def _load_actio_model_call_refs(
    config: AppConfig,
    actio_config: ActioIntegrationConfig,
) -> dict[str, object]:
    if actio_config.models is not None:
        return actio_config.models
    if actio_config.models_config_path is not None:
        legacy_base_dir = config.config_dir / "actio"
        source_path = _resolve_path(actio_config.models_config_path, legacy_base_dir)
        return _load_yaml_mapping(source_path)
    return config.share.actio.models.model_dump()


def _actio_model_call_refs_for_materialization(
    config: AppConfig,
    actio_config: ActioIntegrationConfig,
) -> dict[str, object]:
    raw = dict(_load_actio_model_call_refs(config, actio_config))
    vlm = raw.get("vlm")
    if isinstance(vlm, str):
        raw["vlm"] = {
            "ref": vlm,
            "timeout_seconds": config.share.actio.vlm_timeout_seconds,
        }
    elif isinstance(vlm, dict):
        raw["vlm"] = {
            **vlm,
            "timeout_seconds": vlm.get("timeout_seconds", config.share.actio.vlm_timeout_seconds),
        }
    toolkit_asr = _selected_actio_toolkit_asr_model_section(actio_config, selector="indexing")
    if toolkit_asr is not None:
        raw["asr"] = _apply_actio_gateway_provider_defaults(toolkit_asr, config)
    if "ocr" not in raw:
        toolkit_ocr = _selected_actio_toolkit_ocr_model_section(actio_config)
        if (
            toolkit_ocr is not None
            and isinstance(toolkit_ocr, dict)
            and not toolkit_ocr.get("ref")
            and not toolkit_ocr.get("model_name")
            and _actio_toolkit_selected(actio_config, "ocr") == "vlm"
        ):
            raw["ocr"] = config.share.actio.models.vlm
        elif toolkit_ocr is not None:
            raw["ocr"] = _apply_actio_gateway_provider_defaults(toolkit_ocr, config)
        elif _actio_toolkit_selected(actio_config, "ocr") == "vlm":
            raw["ocr"] = config.share.actio.models.vlm
    return raw


def _actio_toolkit_selected(actio_config: ActioIntegrationConfig, toolkit_name: str) -> str | None:
    if actio_config.toolkit is None:
        return None
    toolkit = actio_config.toolkit.get(toolkit_name)
    if not isinstance(toolkit, dict):
        return None
    selected = toolkit.get("selected")
    return selected if isinstance(selected, str) and selected else None


def _apply_actio_gateway_provider_defaults(section: object, config: AppConfig) -> object:
    """Fill missing gateway env wiring from app ``model_providers`` for Actio toolkit sections."""

    if isinstance(section, str):
        return section
    if not isinstance(section, dict):
        return section
    if "ref" in section or not section.get("model_name"):
        return section

    provider = config.model_providers.get("asr") or config.model_providers.get("private")
    if provider is None:
        return section
    updated = dict(section)
    if not updated.get("base_url") and not updated.get("base_url_env"):
        updated["base_url_env"] = provider.base_url_env
    if not updated.get("api_key") and not updated.get("api_key_env"):
        updated["api_key_env"] = provider.api_key_env
    return updated


def _selected_actio_toolkit_asr_model_section(
    actio_config: ActioIntegrationConfig,
    *,
    selector: str | None = None,
) -> object | None:
    if selector == "indexing":
        return _selected_actio_toolkit_model_section(
            actio_config,
            "asr",
            selected_key="indexing_selected",
        )
    if selector == "qa":
        return _selected_actio_toolkit_model_section(
            actio_config,
            "asr",
            selected_key="qa_selected",
        )
    return _selected_actio_toolkit_model_section(actio_config, "asr")


def _actio_asr_runtime_fields(section: object) -> dict[str, object]:
    """Copy ASR runtime controls from the selected Actio toolkit strategy."""

    if not isinstance(section, dict):
        return {}
    copied: dict[str, object] = {}
    for key in (
        "region_mode",
        "transport",
        "max_upload_mb",
        "chunk_seconds",
        "timeout_seconds",
        "job_timeout_seconds",
        "preparation_timeout_seconds",
        "queue_timeout_seconds",
        "transcription_timeout_seconds",
        "poll_interval_seconds",
    ):
        if key in section:
            value = section[key]
            if key in {
                "timeout_seconds",
                "job_timeout_seconds",
                "poll_interval_seconds",
                "preparation_timeout_seconds",
                "queue_timeout_seconds",
                "transcription_timeout_seconds",
            }:
                value = float(value)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f"actio.asr.{key} must be finite and positive")
            copied[key] = value
    return copied


def _selected_actio_toolkit_ocr_model_section(
    actio_config: ActioIntegrationConfig,
) -> object | None:
    return _selected_actio_toolkit_model_section(actio_config, "ocr")


def _selected_actio_toolkit_model_section(
    actio_config: ActioIntegrationConfig,
    toolkit_name: str,
    *,
    selected_key: str = "selected",
) -> object | None:
    if actio_config.toolkit is None:
        return None
    toolkit = actio_config.toolkit.get(toolkit_name)
    if not isinstance(toolkit, dict):
        return None
    selected = toolkit.get(selected_key)
    if not isinstance(selected, str) or not selected:
        return None
    strategy = toolkit.get(selected)
    if not isinstance(strategy, dict):
        if toolkit_name == "ocr" and selected == "vlm":
            return {}
        raise ValueError(f"actio.toolkit.{toolkit_name} {selected_key} strategy is invalid")
    if "ref" in strategy or "model_name" in strategy:
        return strategy
    if toolkit_name == "ocr" and selected == "vlm":
        return {}
    return None


def _resolve_multimodal_tool_configs(
    multimodal: MultimodalToolConfig,
    config: AppConfig,
    actio_config: ActioIntegrationConfig | None,
) -> MultimodalToolConfig:
    return multimodal.model_copy(
        update={
            "review_model": resolve_tool_model_config(
                multimodal.review_model,
                config.model_providers,
                field_name="tools.multimodal.review_model.ref",
            ),
            "asr": (
                resolve_tool_model_config_from_actio(
                    multimodal.asr,
                    config,
                    config.model_providers,
                    actio_config,
                    field_name="tools.multimodal.asr.ref",
                )
                if multimodal.asr is not None
                else None
            ),
            "caption": (
                resolve_tool_model_config(
                    multimodal.caption,
                    config.model_providers,
                    field_name="tools.multimodal.caption.ref",
                )
                if multimodal.caption is not None
                else None
            ),
            "ocr": (
                resolve_tool_model_config_from_actio(
                    multimodal.ocr,
                    config,
                    config.model_providers,
                    actio_config,
                    field_name="tools.multimodal.ocr.ref",
                )
                if multimodal.ocr is not None
                else None
            ),
            "video_discovery": (
                resolve_tool_model_config(
                    multimodal.video_discovery,
                    config.model_providers,
                    field_name="tools.multimodal.video_discovery.ref",
                )
                if multimodal.video_discovery is not None
                else None
            ),
            "locate_anything": (
                resolve_locate_anything_config_from_actio(
                    multimodal.locate_anything,
                    actio_config,
                    config=config,
                    field_name="tools.multimodal.locate_anything.ref",
                )
                if multimodal.locate_anything is not None
                else None
            ),
        }
    )


def _render_actio_model_section(
    section: object,
    model_providers: dict[str, ModelProviderConfig],
    *,
    field_name: str,
    passthrough_keys: tuple[str, ...] = (),
) -> dict[str, object]:
    resolved = _resolve_actio_tool_model_section(section, model_providers, field_name=field_name)
    rendered: dict[str, object] = {
        "model_name": resolved["model"],
        "base_url": _actio_value_from_provider(
            field_name=f"{field_name}.base_url",
            value=resolved.get("base_url"),
            env_name=resolved.get("base_url_env"),
        ),
        "api_key": _actio_value_from_provider(
            field_name=f"{field_name}.api_key",
            value=resolved.get("api_key"),
            env_name=resolved.get("api_key_env"),
        ),
    }
    if isinstance(section, dict):
        for key in passthrough_keys:
            if key in section:
                rendered[key] = section[key]
    return rendered


def _resolve_actio_tool_model_section(
    section: object,
    model_providers: dict[str, ModelProviderConfig],
    *,
    field_name: str,
) -> dict[str, str | None]:
    if isinstance(section, str):
        ref = section
    elif isinstance(section, dict):
        explicit = {
            key: str(section.get(key, "")).strip()
            for key in ("model_name", "base_url", "base_url_env", "api_key", "api_key_env")
        }
        if (
            explicit["model_name"]
            and (explicit["base_url"] or explicit["base_url_env"])
            and (explicit["api_key"] or explicit["api_key_env"])
        ):
            return {
                "model": explicit["model_name"],
                "base_url": explicit["base_url"] or None,
                "base_url_env": explicit["base_url_env"] or None,
                "api_key": explicit["api_key"] or None,
                "api_key_env": explicit["api_key_env"] or None,
            }
        ref_value = section.get("ref")
        if not isinstance(ref_value, str):
            raise ValueError(f"{field_name} requires ref or model_name/base_url/api_key(_env)")
        ref = ref_value
    else:
        raise ValueError(f"{field_name} must be a model ref string or mapping")

    provider_name, model = _split_model_ref_literal(ref, field_name=f"{field_name}.ref")
    provider_config = _require_model_provider(
        model_providers,
        provider_name,
        f"{field_name}.ref",
    )
    return {
        "model": model,
        "base_url": provider_config.base_url,
        "base_url_env": provider_config.base_url_env,
        "api_key": None,
        "api_key_env": provider_config.api_key_env,
    }


def _write_yaml_mapping(path: Path, data: dict[str, object]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    return data


def resolve_files_root(config: AppConfig) -> Path:
    """Effective files root: ``share.files_root`` mirrored on ``app.files_root`` when set.

    Multi-host deployments must set ``VIKNOW_FILES_ROOT`` to a shared POSIX mount;
    the state-dir fallback only serves single-host development and tests.
    """

    if config.app.files_root is not None:
        return config.app.files_root
    return config.app.state_dir / "files"


def resolve_sessions_root(files_root: Path) -> Path:
    """Session workspace subtree under the unified files root."""

    return files_root / FILES_ROOT_SESSIONS_DIR


def resolve_knowledge_uploads_root(files_root: Path) -> Path:
    """Knowledge upload source-file subtree under the unified files root."""

    return files_root / FILES_ROOT_KNOWLEDGE_DIR / FILES_ROOT_KNOWLEDGE_UPLOADS_DIR


def resolve_knowledge_libraries_root(files_root: Path) -> Path:
    """HyperRAG ``working_dir`` subtree under the unified files root."""

    return files_root / FILES_ROOT_KNOWLEDGE_DIR / FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR


def resolve_knowledge_exports_root(files_root: Path) -> Path:
    """Knowledge migration package subtree under the unified files root."""

    return files_root / FILES_ROOT_KNOWLEDGE_DIR / FILES_ROOT_KNOWLEDGE_EXPORTS_DIR


def resolve_session_sqlite_path(*, state_dir: Path) -> Path:
    return state_dir / "sessions.sqlite"


def resolve_checkpointer_sqlite_path(*, state_dir: Path) -> Path:
    return state_dir / "deepagents" / "checkpoints.sqlite"


def resolve_session_postgres_dsn(persistence: PersistenceConfig) -> str:
    return _resolve_postgres_component_dsn(
        persistence,
        dsn_env=DEFAULT_SESSION_DSN_ENV,
        schema=lambda pg: pg.session_schema,
    )


def resolve_checkpointer_postgres_dsn(persistence: PersistenceConfig) -> str:
    return _resolve_postgres_component_dsn(
        persistence,
        dsn_env=DEFAULT_CHECKPOINT_DSN_ENV,
        schema=lambda pg: pg.checkpoint_schema,
    )


def resolve_memory_postgres_dsn(persistence: PersistenceConfig) -> str:
    return _resolve_postgres_component_dsn(
        persistence,
        dsn_env=DEFAULT_MEMORY_DSN_ENV,
        schema=lambda pg: pg.memory_schema,
    )


def resolve_scheduler_postgres_dsn(persistence: PersistenceConfig) -> str:
    """Resolve scheduler metrics DSN from ``persistence.postgres`` (no dedicated DSN env)."""

    if persistence.postgres is None:
        raise RuntimeError("persistence.postgres is required for postgres DSNs")
    return append_postgres_search_path(
        _resolve_postgres_base_dsn(persistence.postgres),
        persistence.postgres.scheduler_schema,
    )


def resolve_learning_postgres_dsn(persistence: PersistenceConfig) -> str:
    """Resolve the dedicated learning schema DSN from shared persistence config."""

    if persistence.postgres is None:
        raise RuntimeError("persistence.postgres is required for learning DSN")
    return append_postgres_search_path(
        _resolve_postgres_base_dsn(persistence.postgres),
        persistence.postgres.learning_schema,
    )


def _resolve_files_root_setting(
    value: Path | None,
    *,
    base_dir: Path,
    field_name: str = "app.files_root",
) -> Path | None:
    """Resolve a files root path; ``VIKNOW_FILES_ROOT`` wins over the config value."""

    raw = os.getenv(FILES_ROOT_ENV)
    if raw is None or not raw.strip():
        raw = str(value) if value is not None else None
        if raw is not None and _ENV_TOKEN_RE.search(raw):
            raw = _expand_env_tokens(raw, field_name=field_name, required=False)
    if raw is None or not raw.strip():
        return None
    return _resolve_path(Path(raw.strip()), base_dir)


def _resolve_app_config_paths(config: AppConfig) -> AppConfig:
    base_dir = config.config_dir
    resolved_files_root = _resolve_files_root_setting(
        config.share.files_root,
        base_dir=base_dir,
        field_name="share.files_root",
    )
    share = config.share.model_copy(update={"files_root": resolved_files_root})
    app = config.app.model_copy(
        update={
            "state_dir": _resolve_path(config.app.state_dir, base_dir),
            "files_root": resolved_files_root,
        }
    )
    api = config.api.model_copy(
        update={"static_dir": _resolve_path(config.api.static_dir, base_dir)}
    )
    knowledge = None
    hyperrag_config_path: Path | None = None
    if config.knowledge is not None:
        hyperrag_config_path = _resolve_path(config.knowledge.hyperrag.config_path, base_dir)
        hyperrag = config.knowledge.hyperrag.model_copy(
            update={
                "config_path": hyperrag_config_path,
                "repo_root": (
                    _resolve_path(config.knowledge.hyperrag.repo_root, base_dir)
                    if config.knowledge.hyperrag.repo_root is not None
                    else None
                ),
                "workspace_root": (
                    _resolve_path(config.knowledge.hyperrag.workspace_root, base_dir)
                    if config.knowledge.hyperrag.workspace_root is not None
                    else None
                ),
            }
        )
        knowledge = config.knowledge.model_copy(update={"hyperrag": hyperrag})
    persistence = _resolve_persistence_config(
        config.persistence,
        base_dir=base_dir,
        hyperrag_config_path=hyperrag_config_path,
    )
    agent = config.agent
    deepagents = agent.deepagents
    deepagents_update: dict[str, object] = {}
    if deepagents.state_root is not None:
        deepagents_update["state_root"] = _resolve_path(deepagents.state_root, base_dir)
    checkpointer_update: dict[str, object] = {
        "backend": persistence.backend,
    }
    if persistence.backend == PersistenceBackend.SQLITE:
        checkpointer_update["sqlite_path"] = resolve_checkpointer_sqlite_path(
            state_dir=app.state_dir,
        )
    elif persistence.backend == PersistenceBackend.POSTGRES:
        checkpointer_update["postgres_dsn"] = resolve_checkpointer_postgres_dsn(persistence)
    deepagents_update["checkpointer"] = deepagents.checkpointer.model_copy(
        update=checkpointer_update
    )
    workspace_update: dict[str, object] = {}
    if deepagents.workspace.root_dir is not None:
        workspace_update["root_dir"] = _resolve_path(deepagents.workspace.root_dir, base_dir)
    if workspace_update:
        deepagents_update["workspace"] = deepagents.workspace.model_copy(update=workspace_update)
    skill_dirs = [_resolve_path(path, base_dir) for path in agent.skill_dirs]
    profile_dir = _resolve_path(agent.profile_dir, base_dir)
    hermes = agent.hermes
    if hermes.home is not None:
        hermes = hermes.model_copy(update={"home": _resolve_path(hermes.home, base_dir)})
    agent = agent.model_copy(
        update={
            "profile_dir": profile_dir,
            "skill_dirs": skill_dirs,
            "deepagents": deepagents.model_copy(update=deepagents_update),
            "hermes": hermes,
        }
    )
    admin = config.admin
    if admin.username is not None or admin.password is not None:
        admin = admin.model_copy(
            update={
                "username": _maybe_expand_config_string(
                    admin.username, field_name="admin.username"
                ),
                "password": _maybe_expand_config_string(
                    admin.password, field_name="admin.password"
                ),
            }
        )
    skill_optimization = config.self_learning_skill_optimization
    skill_optimization_paths = {
        field_name: _resolve_path(path, base_dir)
        for field_name in (
            "skillopt_package_root",
            "skillopt_config_path",
            "experiment_root",
            "base_config_path",
            "source_skill_root",
            "source_profile_path",
        )
        if (path := getattr(skill_optimization, field_name)) is not None
    }
    skill_optimization = skill_optimization.model_copy(update=skill_optimization_paths)
    resolved = config.model_copy(
        update={
            "app": app,
            "api": api,
            "share": share,
            "knowledge": knowledge,
            "agent": agent,
            "persistence": persistence,
            "admin": admin,
            "self_learning_skill_optimization": skill_optimization,
        }
    )
    resolved._config_path = config.config_path
    return resolved


def _expand_persistence_backend_tokens(raw: dict) -> dict:
    """Expand env tokens in ``persistence.backend`` when present; literals pass through."""

    persistence = raw.get("persistence")
    if not isinstance(persistence, dict):
        return raw
    backend = persistence.get("backend")
    if not isinstance(backend, str) or not _ENV_TOKEN_RE.search(backend):
        return raw
    expanded = _expand_env_tokens(backend, field_name="persistence.backend", required=True)
    if expanded is None:
        raise RuntimeError("persistence.backend env expansion returned empty")
    return {**raw, "persistence": {**persistence, "backend": expanded.strip().lower()}}


def _resolve_persistence_backend() -> PersistenceBackend:
    raw = os.getenv(DEFAULT_PERSISTENCE_BACKEND_ENV)
    if raw is None or not raw.strip():
        raise RuntimeError(
            f"environment variable is required: {DEFAULT_PERSISTENCE_BACKEND_ENV} (sqlite|postgres)"
        )
    try:
        return PersistenceBackend(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PersistenceBackend)
        raise RuntimeError(f"{DEFAULT_PERSISTENCE_BACKEND_ENV} must be one of: {allowed}") from exc


def _resolve_persistence_config(
    persistence: PersistenceConfig,
    *,
    base_dir: Path,
    hyperrag_config_path: Path | None = None,
) -> PersistenceConfig:
    _ = base_dir
    backend = persistence.backend or _resolve_persistence_backend()
    postgres = persistence.postgres
    if postgres is not None or backend == PersistenceBackend.POSTGRES:
        postgres = _merge_postgres_from_hyperrag(
            postgres or PostgresPersistenceConfig(),
            hyperrag_config_path=hyperrag_config_path,
        )
        # Only require POSTGRES_* env tokens when actually using postgres.
        # sqlite loads may keep unresolved placeholders in unused fields.
        postgres = _expand_postgres_persistence_config(
            postgres,
            required=(backend == PersistenceBackend.POSTGRES),
        )
    if backend == PersistenceBackend.POSTGRES:
        if postgres is None:
            raise RuntimeError("persistence.postgres is required when backend is postgres")
        _require_postgres_connection_fields(postgres)
    return PersistenceConfig(backend=backend, postgres=postgres)


def _merge_postgres_from_hyperrag(
    postgres: PostgresPersistenceConfig,
    *,
    hyperrag_config_path: Path | None,
) -> PostgresPersistenceConfig:
    """Fill omitted connection fields from HyperRAG ``env.POSTGRES_*``."""

    hyperrag_env = _load_hyperrag_postgres_env(hyperrag_config_path)
    updates: dict[str, object] = {}
    if postgres.host is None and "POSTGRES_HOST" in hyperrag_env:
        updates["host"] = hyperrag_env["POSTGRES_HOST"]
    if postgres.port is None and "POSTGRES_PORT" in hyperrag_env:
        updates["port"] = int(hyperrag_env["POSTGRES_PORT"])
    if postgres.user is None and "POSTGRES_USER" in hyperrag_env:
        updates["user"] = hyperrag_env["POSTGRES_USER"]
    if postgres.password is None and "POSTGRES_PASSWORD" in hyperrag_env:
        updates["password"] = hyperrag_env["POSTGRES_PASSWORD"]
    if postgres.database is None and "POSTGRES_DATABASE" in hyperrag_env:
        updates["database"] = hyperrag_env["POSTGRES_DATABASE"]
    if not updates:
        return postgres
    return postgres.model_copy(update=updates)


def _load_hyperrag_postgres_env(hyperrag_config_path: Path | None) -> dict[str, str]:
    if hyperrag_config_path is None or not hyperrag_config_path.is_file():
        return {}
    raw = _load_yaml_mapping(hyperrag_config_path)
    env = raw.get("env")
    if not isinstance(env, dict):
        return {}
    values: dict[str, str] = {}
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DATABASE",
    ):
        value = env.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        expanded = _expand_env_tokens(
            text,
            field_name=f"hyperrag.env.{key}",
            required=False,
        )
        if expanded is not None and expanded.strip():
            values[key] = expanded.strip()
    return values


def _require_postgres_connection_fields(postgres: PostgresPersistenceConfig) -> None:
    missing = [
        name
        for name in ("host", "port", "user", "password", "database")
        if getattr(postgres, name) is None
    ]
    if missing:
        raise RuntimeError(
            "persistence.postgres missing "
            + ", ".join(missing)
            + "; set them in app.yaml or hyperrag.yml env.POSTGRES_*"
        )


def _expand_postgres_persistence_config(
    postgres: PostgresPersistenceConfig,
    *,
    required: bool = True,
) -> PostgresPersistenceConfig:
    """Expand env tokens in postgres persistence string fields."""

    def expand(field_name: str, value: str | None) -> str | None:
        if value is None:
            return None
        expanded = _expand_env_tokens(
            value,
            field_name=f"persistence.postgres.{field_name}",
            required=required,
        )
        if expanded is None or not expanded.strip():
            if required:
                raise RuntimeError(f"persistence.postgres.{field_name} resolved empty")
            return None
        return expanded.strip()

    port = postgres.port
    if isinstance(port, str):
        expanded_port = expand("port", port)
        port = int(expanded_port) if expanded_port else None
    elif port is None:
        raw_port = os.getenv("POSTGRES_PORT", "").strip()
        if raw_port:
            port = int(raw_port)
        elif required:
            port = 5432

    return postgres.model_copy(
        update={
            "host": expand("host", postgres.host),
            "port": port,
            "user": expand("user", postgres.user),
            "password": expand("password", postgres.password),
            "database": expand("database", postgres.database),
            "session_schema": expand("session_schema", postgres.session_schema),
            "checkpoint_schema": expand("checkpoint_schema", postgres.checkpoint_schema),
            "memory_schema": expand("memory_schema", postgres.memory_schema),
            "scheduler_schema": expand("scheduler_schema", postgres.scheduler_schema),
            "juicefs_schema": expand("juicefs_schema", postgres.juicefs_schema),
        }
    )


def _resolve_postgres_base_dsn(postgres: PostgresPersistenceConfig) -> str:
    _require_postgres_connection_fields(postgres)
    assert postgres.host is not None
    assert postgres.port is not None
    assert postgres.user is not None
    assert postgres.password is not None
    assert postgres.database is not None
    return (
        f"postgresql://{quote_plus(postgres.user)}:{quote_plus(postgres.password)}"
        f"@{postgres.host}:{postgres.port}/{postgres.database}"
    )


def _resolve_postgres_component_dsn(
    persistence: PersistenceConfig,
    *,
    dsn_env: str,
    schema: Callable[[PostgresPersistenceConfig], str],
) -> str:
    """Resolve a Postgres DSN scoped to one ViKnow schema within the shared database."""

    explicit_dsn = os.getenv(dsn_env)
    if explicit_dsn is not None and explicit_dsn.strip():
        return explicit_dsn.strip()

    if persistence.postgres is None:
        raise RuntimeError("persistence.postgres is required for postgres DSNs")
    schema_name = schema(persistence.postgres)
    return append_postgres_search_path(
        _resolve_postgres_base_dsn(persistence.postgres), schema_name
    )


def append_postgres_search_path(dsn: str, schema: str) -> str:
    """Append a libpq ``search_path`` option to a Postgres connection URL.

    ``public`` is included so shared extension types (e.g. pgvector ``vector`` in
    ``public``) remain visible while ViKnow tables live in a dedicated schema.
    """

    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    search_path_option = f"-csearch_path={schema},public"
    existing_options = query.get("options", "").strip()
    query["options"] = (
        f"{existing_options} {search_path_option}".strip()
        if existing_options
        else search_path_option
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _resolve_path(path: Path, base_dir: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _resolve_default_actio_config_dir(config: AppConfig) -> Path:
    configured_default = _resolve_path(DEFAULT_ACTIO_CONFIG_DIR, config.config_dir)
    candidates = [configured_default]
    if config.knowledge is not None and config.knowledge.hyperrag.repo_root is not None:
        repo_root = _resolve_path(config.knowledge.hyperrag.repo_root, config.config_dir)
        candidates.append((repo_root.parent / "actio" / "configs").resolve())
        candidates.append((repo_root / "external" / "actio" / "configs").resolve())
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return configured_default


def _config_value(name: str, explicit: str | None, env_name: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    if env_name:
        value = os.getenv(env_name)
        if value and value.strip():
            return value.strip()
    return None


def _resolved_env_value(env_name: str | None) -> str | None:
    if not env_name:
        return None
    value = os.getenv(env_name)
    if value and value.strip():
        return value.strip()
    return None


def _validate_config_model_ref(
    model_config: AgentModelConfig,
    model_providers: dict[str, ModelProviderConfig],
    field_name: str,
) -> None:
    if model_config.ref:
        provider_name, _model = _split_model_ref(
            model_config.ref,
            field_name=f"{field_name}.ref",
            required=False,
        )
        if provider_name is not None:
            _require_model_provider(model_providers, provider_name, f"{field_name}.ref")
        return
    if model_config.provider_name:
        _require_model_provider(
            model_providers,
            model_config.provider_name,
            f"{field_name}.provider_name",
        )


def _validate_option_model_ref(
    option: AgentModelOption,
    model_providers: dict[str, ModelProviderConfig],
    field_name: str,
) -> None:
    if option.ref:
        provider_name, _model = _split_model_ref(
            option.ref,
            field_name=f"{field_name}.ref",
            required=False,
        )
        if provider_name is not None:
            _require_model_provider(model_providers, provider_name, f"{field_name}.ref")
        return
    if option.provider_name:
        _require_model_provider(
            model_providers,
            option.provider_name,
            f"{field_name}.provider_name",
        )


def _require_model_provider(
    model_providers: dict[str, ModelProviderConfig],
    provider_name: str,
    field_name: str,
) -> ModelProviderConfig:
    provider_config = model_providers.get(provider_name)
    if provider_config is None:
        raise ValueError(f"{field_name} references unknown model_providers entry: {provider_name}")
    return provider_config


def _split_model_ref(
    ref: str,
    *,
    field_name: str,
    required: bool,
) -> tuple[str | None, str | None]:
    provider_name, separator, model = ref.partition(":")
    provider_name = provider_name.strip()
    model = model.strip()
    if not separator or not provider_name or not model:
        if required:
            raise ValueError(f"{field_name} must use provider:model")
        return None, None
    model = _expand_env_tokens(model, field_name=field_name, required=required)
    if not model:
        if required:
            raise RuntimeError(f"{field_name} model name is empty")
        return provider_name, None
    return provider_name, model


def _split_model_ref_literal(ref: str, *, field_name: str) -> tuple[str, str]:
    provider_name, separator, model = ref.partition(":")
    provider_name = provider_name.strip()
    model = model.strip()
    if not separator or not provider_name or not model:
        raise ValueError(f"{field_name} must use provider:model")
    return provider_name, model


def _model_fields_from_ref_model(model: str, *, field_name: str) -> dict[str, str | None]:
    env_match = _ENV_TOKEN_RE.fullmatch(model)
    if env_match:
        return {"model": None, "model_env": env_match.group(1)}
    if _ENV_TOKEN_RE.search(model):
        expanded = _expand_env_tokens(model, field_name=field_name, required=True)
        return {"model": expanded, "model_env": None}
    return {"model": model, "model_env": None}


def _actio_value_from_provider(
    field_name: str,
    value: str | None,
    env_name: str | None,
) -> str:
    if value and value.strip():
        return value.strip()
    if env_name and env_name.strip():
        return f"${{{env_name.strip()}}}"
    raise ValueError(f"{field_name} requires {field_name.split('.')[-1]} or env")


def _runtime_value_from_provider(*, value: str | None, env_name: str | None) -> str | None:
    if value and value.strip():
        return value.strip()
    if env_name and env_name.strip():
        env_value = os.getenv(env_name.strip())
        if env_value and env_value.strip():
            return env_value.strip()
        return f"${{{env_name.strip()}}}"
    return None


def _load_repo_env_values(config: AppConfig) -> dict[str, str]:
    values: dict[str, str] = {}
    repo_root = config.config_dir.parent.parent
    for path in (repo_root / ".env", repo_root / ".env.local"):
        values.update(parse_env_file(path))
    return values


def _expand_env_tokens_in_object(
    value: object,
    *,
    field_name: str,
    required: bool,
    env_values: dict[str, str] | None = None,
) -> object:
    if isinstance(value, str):
        return _expand_env_tokens(
            value,
            field_name=field_name,
            required=required,
            env_values=env_values,
        )
    if isinstance(value, dict):
        return {
            key: _expand_env_tokens_in_object(
                item,
                field_name=f"{field_name}.{key}",
                required=required,
                env_values=env_values,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _expand_env_tokens_in_object(
                item,
                field_name=f"{field_name}[{index}]",
                required=required,
                env_values=env_values,
            )
            for index, item in enumerate(value)
        ]
    return value


def _maybe_expand_config_string(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not _ENV_TOKEN_RE.search(stripped):
        return stripped
    expanded = _expand_env_tokens(stripped, field_name=field_name, required=True)
    return expanded.strip() if expanded and expanded.strip() else None


def _expand_env_tokens(
    value: str,
    *,
    field_name: str,
    required: bool,
    env_values: dict[str, str] | None = None,
) -> str | None:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = (env_values or {}).get(env_name)
        if env_value is None:
            env_value = os.getenv(env_name)
        if env_value is None or not env_value.strip():
            missing.append(env_name)
            return ""
        return env_value.strip()

    expanded = _ENV_TOKEN_RE.sub(replace, value).strip()
    if missing:
        if required:
            raise RuntimeError(
                f"{field_name} references unset environment variable(s): " + ", ".join(missing)
            )
        return None
    return expanded
