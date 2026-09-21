"""HTTP API request contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from viknow.core.eval.candidate_evidence import EvidenceCollectionTask
from viknow.core.runtime.domain import KnowledgeAssetSelection


def _normalize_library_ids(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        library_id = str(item).strip()
        if not library_id:
            raise ValueError("library_ids must not contain empty values")
        if library_id not in seen:
            normalized.append(library_id)
            seen.add(library_id)
    if len(normalized) > 100:
        raise ValueError("library_ids supports at most 100 values")
    return normalized


class SubmitRequest(BaseModel):
    question: str = Field(min_length=1)
    session_id: str | None = None
    user_id: str = Field(default="web-user", min_length=1)
    workspace_id: str | None = "web"
    agent_model: str | None = Field(default=None, min_length=1)


class CreateRunRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: UUID | None = None
    user_id: UUID
    profile_id: str | None = Field(default=None, min_length=1)
    agent_model: str | None = Field(default=None, min_length=1)
    use_precompiled_knowledge: bool = True
    attachment_file_ids: list[str] | None = None
    library_ids: list[str] | None = None

    @field_validator("library_ids")
    @classmethod
    def validate_library_ids(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_library_ids(value)


class SessionFileResponse(BaseModel):
    file_id: str
    file_name: str
    mime_type: str | None = None
    size_bytes: int
    kind: str
    status: str
    virtual_path: str
    uploaded_at: datetime


class SessionFileListResponse(BaseModel):
    files: list[SessionFileResponse]
    upload_limits: dict[str, object]
    parse_limits: dict[str, object]


class CreateRunResponse(BaseModel):
    run_id: str
    session_id: str
    workspace_id: str
    profile_id: str


class SubmitLearningFeedbackRequest(BaseModel):
    disposition: Literal["helpful", "unhelpful", "withdrawn"]


class LearningFeedbackResponse(BaseModel):
    run_id: str
    revision: int
    disposition: Literal["helpful", "unhelpful", "withdrawn"]
    prior_disposition: Literal["helpful", "unhelpful"] | None = None


class EvalRunRequest(BaseModel):
    query: str = Field(min_length=1)
    profile_id: str | None = Field(
        default=None,
        min_length=1,
        description="Optional runtime profile; evidence-oracle requires approved selected assets.",
    )
    timeout_seconds: float = Field(default=300.0, gt=0)
    agent_model: str | None = Field(default=None, min_length=1)
    trace_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional eval trace id. A 32-character lowercase hex value is used as-is; "
            "any other value is treated as a deterministic external seed."
        ),
    )
    trace_name: str | None = Field(
        default=None,
        min_length=1,
        description="Optional Langfuse trace name for this eval run.",
    )
    user_id: UUID | None = Field(
        default=None,
        description="Optional user for this eval run; use the same id in the web UI.",
    )
    gold_trace: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional skill-flow gold tool trace used to simulate tool results.",
    )
    selected_knowledge_assets: list[KnowledgeAssetSelection] = Field(
        default_factory=list,
        description="Knowledge assets already selected by the user for this eval run.",
    )
    library_ids: list[str] | None = None
    evidence_collection_task: EvidenceCollectionTask | None = Field(
        default=None,
        description="Approved Gate-A task required for a scoped Evidence Oracle run.",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        return stripped

    @field_validator("library_ids")
    @classmethod
    def validate_library_ids(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_library_ids(value)

    @model_validator(mode="after")
    def validate_evidence_oracle_task(self) -> EvalRunRequest:
        if self.library_ids is not None:
            allowed_library_ids = set(self.library_ids)
            if any(
                asset.library_id not in allowed_library_ids
                for asset in self.selected_knowledge_assets
            ):
                raise ValueError("selected_knowledge_assets must be within library_ids")
        task = self.evidence_collection_task
        if task is None:
            return self
        if self.profile_id != "evidence-oracle":
            raise ValueError("evidence_collection_task requires profile_id=evidence-oracle")
        if task.gate_a_status != "approved":
            raise ValueError("evidence_collection_task requires Gate A approval")
        if self.library_ids is not None and task.scope.library_id not in self.library_ids:
            raise ValueError("evidence_collection_task scope must be within library_ids")
        task_assets = {(task.scope.library_id, asset_id) for asset_id in task.scope.asset_ids}
        requested_assets = {
            (asset.library_id, asset.asset_id) for asset in self.selected_knowledge_assets
        }
        if task_assets and requested_assets and requested_assets != task_assets:
            raise ValueError("selected_knowledge_assets must exactly match evidence task scope")
        return self


from viknow.core.eval.trace import EvalRunTrace  # noqa: E402


class EvalRunResponse(EvalRunTrace):
    """Structured eval trace returned by POST /api/v1/eval/runs."""


class InspectToolEvalRunCaseRequest(BaseModel):
    case_id: str = Field(min_length=1)
    tool: Literal[
        "document_inspect", "image_inspect", "video_inspect", "video_inspect_2_0", "visual_ground"
    ]
    arguments: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    judge_timeout_seconds: float | None = Field(default=None, gt=0)


class InspectToolEvalRunCaseResponse(BaseModel):
    case_id: str
    tool: str
    arguments: dict[str, Any]
    metadata: dict[str, Any]
    status: str
    phase: str
    evidence_resolved: bool | None
    resolved_evidence: Any | None = None
    inspect_trace: list[dict[str, Any]]
    tool_result: Any | None = None
    judge_result: Any | None = None
    telemetry: dict[str, Any] = Field(default_factory=dict)
    rule_checks: list[dict[str, Any]] = Field(default_factory=list)
    risk_triage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class InspectToolEvalRejudgeCaseRequest(BaseModel):
    case_id: str = Field(min_length=1)
    tool: Literal[
        "document_inspect", "image_inspect", "video_inspect", "video_inspect_2_0", "visual_ground"
    ]
    arguments: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    frozen_run: dict[str, Any]
    repeat_count: int = Field(default=3, ge=1, le=20)
    judge_timeout_seconds: float | None = Field(default=None, gt=0)


class InspectToolEvalRejudgeCaseResponse(BaseModel):
    case_id: str
    tool: str
    repeat_count: int
    attempts: list[dict[str, Any]]


class AgentModelOptionResponse(BaseModel):
    id: str
    label: str


class AgentModelsResponse(BaseModel):
    default_model: str
    models: list[AgentModelOptionResponse]


class AgentProfileOptionResponse(BaseModel):
    id: str
    label: str
    description: str


class AgentProfilesResponse(BaseModel):
    default_profile: str
    profiles: list[AgentProfileOptionResponse]


class UserResponse(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    display_name: str | None = None
    memory_enabled: bool = False


from viknow.core.memory import UserPreferences  # noqa: E402


class SessionResponse(BaseModel):
    id: str
    user_id: str
    workspace_id: str
    profile_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_run_id: str | None = None
    active_run_id: str | None = None


class CreateKnowledgeIndexParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = Field(default=None, min_length=1)


class CreateKnowledgeIndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    asset_path: str = Field(min_length=1)
    compile_mode: Literal["pre_compile", "fast_ingest"] | None = Field(
        default=None,
        description=(
            "Optional indexing mode: pre_compile for long-term knowledge graph "
            "materialization or fast_ingest for incremental ingestion."
        ),
    )
    params: CreateKnowledgeIndexParams | None = None


class CreateKnowledgeIndexResponse(BaseModel):
    job_id: str
    library_id: str
    asset_id: str
    status: str


class KnowledgeIndexItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    type: Literal["caption", "transcript"]
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> KnowledgeIndexItemResponse:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class KnowledgeAssetIndexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str
    asset_id: str
    index_status: Literal["queued", "running", "completed", "failed", "cancelled"]
    items: list[KnowledgeIndexItemResponse] = Field(default_factory=list)


class KnowledgeIndexJobResponse(BaseModel):
    job_id: str
    library_id: str
    asset_id: str
    status: Literal["pending", "running", "done", "failed"]
    error_message: str | None = None
    metadata: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Index metadata when status is done (e.g. doc_type, page_count for documents, "
            "video_duration for videos). Omitted or null while the job is still running."
        ),
    )


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1)

    @field_validator("library_id", "query")
    @classmethod
    def validate_nonempty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class DeleteKnowledgeIndexRequest(BaseModel):
    library_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)


class DeleteKnowledgeIndexResponse(BaseModel):
    job_id: str
    library_id: str
    asset_id: str
    deleted: bool
    message: str | None = None


class ExportKnowledgePackageRequest(BaseModel):
    workspaces: list[str] | None = None
    company_replacements: dict[str, str] | None = None
    alias_fullname_map: dict[str, str] | None = None


class ExportKnowledgePackageResponse(BaseModel):
    package: dict


class ImportKnowledgePackageRequest(BaseModel):
    package: dict
    company_replacements: dict[str, str] | None = None
    alias_fullname_map: dict[str, str] | None = None


class ImportKnowledgePackageResponse(BaseModel):
    workspace: str
    imported_entities: int
    imported_relations: int
    storage: dict
    vector_storage: dict


class MigrationExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str = Field(min_length=1)


class MigrationJobAcceptedResponse(BaseModel):
    job_id: str
    kind: str
    library_id: str
    status: str


class MigrationJobResponse(BaseModel):
    job_id: str
    kind: str
    library_id: str
    status: str
    stage: str | None = None
    progress: float
    message: str | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MigrationExportFile(BaseModel):
    library_id: str | None = None
    filename: str
    size_bytes: int
    created_at: datetime


class MigrationExportListResponse(BaseModel):
    library_id: str | None = None
    exports: list[MigrationExportFile]
    total: int = 0
    page: int = 1
    page_size: int = 10


class MigrationImportFromExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_library_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    target_library_id: str = Field(min_length=1)


class KnowledgeUploadResponse(BaseModel):
    upload_id: str
    library_id: str
    asset_id: str
    display_name: str
    original_filename: str
    size_bytes: int
    status: str
    failure_kind: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeUploadListResponse(BaseModel):
    uploads: list[KnowledgeUploadResponse]


class DeleteKnowledgeUploadResponse(BaseModel):
    upload_id: str
    deleted: bool


class KnowledgeUploadVideoSummaryResponse(BaseModel):
    upload_id: str
    library_id: str
    asset_id: str
    global_summary: str


class UpdateKnowledgeUploadVideoSummaryRequest(BaseModel):
    global_summary: str


class UpdateKnowledgeUploadVideoSummaryResponse(BaseModel):
    upload_id: str
    library_id: str
    asset_id: str
    updated: bool
    job_id: str | None = None


class VicoachAssessmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    library_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    order: int = Field(ge=1)
    question_type: int = Field(ge=0, le=4)


class VicoachAnswerSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    answer: str | bool | list[str]


class VicoachChapterLocationResponse(BaseModel):
    start: float
    end: float


class VicoachChapterResponse(BaseModel):
    title: str
    description: str
    order: int
    location: VicoachChapterLocationResponse


class VicoachChaptersResponse(BaseModel):
    library_id: str
    asset_id: str
    chapters: list[VicoachChapterResponse]


class VicoachOptionResponse(BaseModel):
    id: str
    text: str


class VicoachQuestionResponse(BaseModel):
    question_id: str
    knowledge_point: str
    question_type: int
    question_markdown: str
    options: list[VicoachOptionResponse] = Field(default_factory=list)


class VicoachAnswerAttemptResponse(BaseModel):
    attempt_id: str
    question_id: str
    answer: str | bool | list[str]
    score: float
    reason: str
    created_at: datetime


class VicoachAssessmentResponse(BaseModel):
    assessment_id: str
    library_id: str
    asset_id: str
    order: int
    question_type: int
    created_at: datetime
    questions: list[VicoachQuestionResponse]
    skipped_knowledge_points: list[str] = Field(default_factory=list)
    attempts: list[VicoachAnswerAttemptResponse] = Field(default_factory=list)


class VicoachAnswerSubmitResponse(BaseModel):
    attempt: VicoachAnswerAttemptResponse
    previous_question_id: str | None = None
    next_question_id: str | None = None


from viknow.core.session.timeline import (  # noqa: E402
    AnswerReviewDetail,
    ReasoningDetail,
    SessionTimeline,
    ToolCallDetail,
)

__all__ = [
    "AnswerReviewDetail",
    "ReasoningDetail",
    "SessionTimeline",
    "ToolCallDetail",
    "UserPreferences",
]
