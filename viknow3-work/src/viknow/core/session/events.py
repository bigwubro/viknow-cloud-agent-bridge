"""Session-backed runtime event persistence."""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from viknow.core.runtime.domain import AgentEvent, AgentEventType
from viknow.core.runtime.events import AgentEventStore
from viknow.core.session.domain import RunStatus
from viknow.core.session.event_policy import EPHEMERAL_EVENT_TYPES, TERMINAL_EVENT_TYPES
from viknow.core.session.store import SessionStore


class SessionEventStore(AgentEventStore):
    """Persists runtime events and mirrors terminal events into run metadata."""

    def __init__(
        self,
        store: SessionStore,
        *,
        on_terminal: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.store = store
        self._on_terminal = on_terminal
        self._ephemeral_by_run: dict[str, list[AgentEvent]] = {}

    def append(self, event: AgentEvent) -> None:
        if event.type in EPHEMERAL_EVENT_TYPES:
            self._ephemeral_by_run.setdefault(event.run_id, []).append(event)
            return

        self.store.append_event(event)
        if event.type == AgentEventType.RUN_COMPLETED:
            self.store.mark_run_terminal(event.run_id, RunStatus.COMPLETED, error=None)
        elif event.type == AgentEventType.RUN_FAILED:
            self.store.mark_run_terminal(
                event.run_id,
                RunStatus.FAILED,
                error=str(event.payload.get("message") or ""),
            )
        elif event.type == AgentEventType.RUN_INTERRUPTED:
            self.store.mark_run_terminal(event.run_id, RunStatus.INTERRUPTED, error=None)

        if event.type in TERMINAL_EVENT_TYPES:
            self._ephemeral_by_run.pop(event.run_id, None)
            if self._on_terminal is not None:
                try:
                    self._on_terminal(event)
                except Exception:
                    logger.opt(exception=True).warning(
                        "terminal event listener failed for run {}",
                        event.run_id,
                    )

    def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        persisted = self.store.events_after(run_id, 0)
        ephemeral = self._ephemeral_by_run.get(run_id, [])
        events = sorted(persisted + ephemeral, key=lambda event: event.sequence)
        return [event for event in events if event.sequence > after_sequence]
