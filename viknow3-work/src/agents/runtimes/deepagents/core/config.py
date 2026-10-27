"""Typed server configuration for ViKnow."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from agents.runtimes.deepagents.core.model_gateway import ModelGatewayRoute
from agents.runtimes.deepagents.core.scheduler.unified_config import (
    SchedulerSectionConfig,
    default_scheduler_section_config,
)


class ModelProvider(StrEnum):
    """Supported model provider families."""

    OPENAI_COMPATIBLE = "openai-compatible"
    PADDLEOCR = "paddleocr"
    GRANITE = "granite"
    ARK = "ark"


class AgentKernelType(StrEnum):
    """Supported agent runtime backends."""

    DEEPAGENTS = "deepagents"


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
    """Storage backend for the checkpointer and mem0."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


DEFAULT_MEMORY_DSN_ENV = "VIKNOW_MEMORY_DSN"


class PostgresPersistenceConfig(_StrictModel):
    """Postgres connection used when mem0 resolves its own DSN.

    Connection fields (``host`` / ``port`` / ``user`` / ``password`` / ``database``)
    are copied from the platform database settings when omitted. Checkpoint uses
    ``VIKNOW_AGENT_DSN`` and does not read this schema.
    """

    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    upload_statement_timeout_seconds: float = Field(default=60, gt=0)
    memory_schema: str = "viknow_memory"



class PersistenceConfig(_StrictModel):
    """App persistence settings.

    Postgres connection fields live under ``postgres``. They are copied from the
    platform database settings when omitted. ``memory_schema`` is the search path
    mem0 uses when it builds a DSN from those fields.
    """

    backend: PersistenceBackend | None = None
    postgres: PostgresPersistenceConfig | None = None



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
    """Workspace settings used by DeepAgents backends."""

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


class ToolConfigs(_StrictModel):
    """All environment-scoped ViKnow tool configs."""

    multimodal: MultimodalToolConfig | None = None
    monitoring: MonitoringApiConfig | None = None



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
    ``agents.runtimes.deepagents.core.memory.config.resolve_mem0_config`` fills qdrant (sqlite) or
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

    def default_model_id(self) -> str | None:
        return self.model.selection_id()


class AppConfig(_StrictModel):
    """Top-level ViKnow server configuration."""

    app: AppSettings
    api: ApiConfig
    share: ShareConfig = Field(default_factory=ShareConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    agent: AgentRuntimeConfig

    _config_path: Path | None = PrivateAttr(default=None)

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
        config = config.model_copy(update={"agent": config.agent, "memory": config.memory})

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


def normalized_model_ref(ref: str, *, required: bool) -> str | None:
    provider_name, model = _split_model_ref(ref, field_name="model ref", required=required)
    if provider_name is None or model is None:
        return None
    return f"{provider_name}:{model}"


def model_name_from_ref(ref: str, *, required: bool) -> str | None:
    _provider_name, model = _split_model_ref(ref, field_name="model ref", required=required)
    return model


def resolve_sessions_root(files_root: Path) -> Path:
    """Session workspace subtree under the unified files root."""

    return files_root / FILES_ROOT_SESSIONS_DIR


def resolve_memory_postgres_dsn(persistence: PersistenceConfig) -> str:
    return _resolve_postgres_component_dsn(
        persistence,
        dsn_env=DEFAULT_MEMORY_DSN_ENV,
        schema=lambda pg: pg.memory_schema,
    )


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
            + "; set persistence.postgres or the component DSN"
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
