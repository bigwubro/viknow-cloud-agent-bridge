"""Optional ViCoach capability settings, independent of knowledge indexing."""

from pydantic import BaseModel, ConfigDict, Field

from .chapters.domain import ChapterConfig


class CourseProcessingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_seconds: float = Field(default=2, gt=0, le=60)
    translation_batch_size: int = Field(default=8, ge=1, le=32)


class ViCoachConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapters: ChapterConfig | None = None
    courses: CourseProcessingConfig = Field(default_factory=CourseProcessingConfig)
