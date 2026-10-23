"""Typed API view of Actio's persisted video observation guidance."""

from typing import Any

from actio.global_context import normalize_global_context
from pydantic import BaseModel, ConfigDict, Field, model_validator


class VideoObservationFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    aspects: list[str]


class VideoGlobalContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_focus: list[VideoObservationFocus] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def validate_context(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        return normalize_global_context(value)
