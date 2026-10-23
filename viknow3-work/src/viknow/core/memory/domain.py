"""User memory domain models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(StrEnum):
    """ViKnow long-term memory categories."""

    USER_PREFERENCE = "user_preference"
    FACT = "fact"
    EPISODIC = "episodic"


class MemorySource(StrEnum):
    """How a memory record was created."""

    USER_EXPLICIT = "user_explicit"
    CONVERSATION_EXTRACTED = "conversation_extracted"


class MemoryScope(BaseModel):
    """Read/write boundary for a user memory kind."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    kind: MemoryKind
    session_id: str | None = None

    @property
    def agent_id(self) -> str:
        return f"viknow:memory:{self.kind.value}"

    def write_metadata(self, **extra: str) -> dict[str, str]:
        metadata = {
            "viknow_kind": self.kind.value,
            **{key: value for key, value in extra.items() if value is not None},
        }
        return metadata

    def read_filters(self) -> dict[str, str]:
        filters = {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "viknow_kind": self.kind.value,
        }
        if self.session_id is not None:
            filters["run_id"] = self.session_id
        return filters


class UserPreferences(BaseModel):
    """User preference fields and memory opt-in."""

    model_config = ConfigDict(extra="forbid")

    memory_enabled: bool = False
    persona: str | None = None
    occupation: str | None = None
    answer_style: str | None = None
    custom_notes: list[str] = Field(default_factory=list)


PREFERENCE_FIELD_LABELS: dict[str, str] = {
    "persona": "人设",
    "occupation": "职业",
    "answer_style": "回答偏好",
}

CUSTOM_NOTE_FIELD = "custom_note"
