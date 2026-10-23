"""Long-term user memory for ViKnow."""

from viknow.core.memory.builder import build_user_memory_service
from viknow.core.memory.domain import (
    MemoryKind,
    MemoryScope,
    MemorySource,
    UserPreferences,
)
from viknow.core.memory.service import (
    Mem0UserMemoryService,
    NoOpUserMemoryService,
    UserMemoryService,
)

__all__ = [
    "Mem0UserMemoryService",
    "MemoryKind",
    "MemoryScope",
    "MemorySource",
    "NoOpUserMemoryService",
    "UserMemoryService",
    "UserPreferences",
    "build_user_memory_service",
]
