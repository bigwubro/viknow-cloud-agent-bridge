"""Contracts for automatic video knowledge-index extraction."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class RealModelEnvironment(BaseModel):
    """Presence checks for real model services used by the extraction pipeline."""

    embedding_api_key_set: bool
    private_api_key_set: bool
    private_base_url_set: bool
    private_model_set: bool

    @classmethod
    def from_env(cls) -> RealModelEnvironment:
        return cls(
            embedding_api_key_set=bool(os.getenv("VIKNOW_EMBEDDING_API_KEY")),
            private_api_key_set=bool(os.getenv("VIKNOW_PRIVATE_API_KEY")),
            private_base_url_set=bool(os.getenv("VIKNOW_PRIVATE_BASE_URL")),
            private_model_set=bool(os.getenv("VIKNOW_PRIVATE_MODEL")),
        )

    def require_ready(self) -> None:
        missing = []
        if not self.embedding_api_key_set:
            missing.append("VIKNOW_EMBEDDING_API_KEY")
        if not self.private_api_key_set:
            missing.append("VIKNOW_PRIVATE_API_KEY")
        if not self.private_base_url_set:
            missing.append("VIKNOW_PRIVATE_BASE_URL")
        if not self.private_model_set:
            missing.append("VIKNOW_PRIVATE_MODEL")
        if missing:
            raise RuntimeError("real model environment is missing: " + ", ".join(missing))


class VideoWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    frame_uri: str

    @field_validator("end_seconds")
    @classmethod
    def end_must_be_after_start(cls, value: float, info) -> float:
        start = info.data.get("start_seconds")
        if start is not None and value <= start:
            raise ValueError("end_seconds must be greater than start_seconds")
        return value

    @computed_field
    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


class EmbeddedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: VideoWindow
    embedding: list[float]
    model: str


class SemanticSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    window_ids: list[str] = Field(min_length=1)
    boundary_reason: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("end_seconds")
    @classmethod
    def end_must_be_after_start(cls, value: float, info) -> float:
        start = info.data.get("start_seconds")
        if start is not None and value <= start:
            raise ValueError("end_seconds must be greater than start_seconds")
        return value

    @computed_field
    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


class SegmentDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slice_id: str
    title: str
    action: str
    semantic: str
    objects: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    state_changes: list[str] = Field(default_factory=list)
    evidence_notes: str
    confidence: float = Field(ge=0, le=1)


class SopStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    document_uri: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class SopAlignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slice_id: str
    sop_ids: list[str]
    confidence: float = Field(ge=0, le=1)
    review_required: bool = False
    reason: str = ""


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning", "review"]
    code: str
    message: str
    location: str | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[ValidationIssue] = Field(default_factory=list)

    @computed_field
    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    def to_markdown(self) -> str:
        if not self.issues:
            return "# 视频索引校验报告\n\n未发现问题。\n"
        lines = ["# 视频索引校验报告", ""]
        for issue in self.issues:
            location = f" `{issue.location}`" if issue.location else ""
            lines.append(f"- **{issue.severity}** `{issue.code}`{location}: {issue.message}")
        lines.append("")
        return "\n".join(lines)
