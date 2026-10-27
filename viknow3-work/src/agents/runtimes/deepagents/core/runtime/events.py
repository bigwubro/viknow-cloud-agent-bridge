"""Runtime event and adapter protocols."""

from __future__ import annotations

from typing import Any, Protocol

from agents.runtimes.deepagents.core.runtime.domain import (
    AgentEvent,
    AgentEventType,
    AgentOp,
    AgentStatus,
    SubmitResult,
    ToolCallRecord,
)

type AgentEventMapping = tuple[AgentEventType, dict[str, Any]]


class AgentEventStore(Protocol):
    """Persistence boundary for standardized runtime events."""

    def append(self, event: AgentEvent) -> None:
        """Persist one standardized event."""

    def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        """Return persisted events for a run after the given sequence."""


class InMemoryAgentEventStore:
    """Process-local event store used until durable storage is introduced."""

    def __init__(self) -> None:
        self._events: dict[str, list[AgentEvent]] = {}

    def append(self, event: AgentEvent) -> None:
        self._events.setdefault(event.run_id, []).append(event)

    def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        return [event for event in self._events.get(run_id, []) if event.sequence > after_sequence]


class AgentEventMapper(Protocol):
    """Maps backend-specific callbacks and results into runtime event data."""

    def map_stream_delta(self, delta: Any) -> AgentEventMapping | None:
        """Map a backend streaming delta to a standardized event."""

    def map_tool_call_started(self, call_id: str, name: str, arguments: Any) -> AgentEventMapping:
        """Map a backend tool-start callback to a standardized event."""

    def map_tool_call_completed(
        self, call_id: str, name: str, arguments: Any, result: Any
    ) -> AgentEventMapping:
        """Map a backend tool-complete callback to a standardized event."""

    def map_tool_call_failed(
        self, call_id: str, name: str, arguments: Any, error: Any
    ) -> AgentEventMapping:
        """Map a backend tool-failure callback to a standardized event."""

    def map_final_answer(self, result: Any) -> str:
        """Extract the final answer text from a backend run result."""

    def map_final_reasoning(self, result: Any) -> str:
        """Extract displayable reasoning text from a backend run result."""

    def map_final_tool_calls(self, result: Any) -> list[ToolCallRecord]:
        """Extract tool calls from a backend run result for callback fallback."""


class AgentRuntime(Protocol):
    """Async contract for agent runtime operations and events."""

    async def submit(self, op: AgentOp) -> SubmitResult:
        """Submit user input or a control operation and return a run id."""

    async def next_event(self, run_id: str | None = None) -> AgentEvent:
        """Return and consume the next standardized event for a run."""

    async def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        """Return persisted events after a sequence for replay."""

    async def get_status(self, run_id: str | None = None) -> AgentStatus:
        """Return runtime-level status or status for the specified run."""

    async def aclose(self) -> None:
        """Release durable resources held by this runtime (idempotent)."""
