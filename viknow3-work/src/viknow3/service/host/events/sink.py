"""EventSink. One sequence stream per run, stored in the run_events table.

The live stream and the replay of a finished run read the same rows. The
condition only wakes readers in this process early; the table is the record.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from viknow3.store import STORE


class EventSink:
    """Written from the agent loop thread, read from request threads."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._version = 0

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        moment: datetime,
        finish: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """With finish, the run row's final fields are written in the same transaction as this event."""
        event = STORE.append_event(run_id, event_type, payload, moment, finish)
        with self._changed:
            self._version += 1
            self._changed.notify_all()
        return event

    def read(self, run_id: str, after_sequence: int) -> list[dict[str, Any]]:
        return STORE.read_events(run_id, after_sequence)

    def version(self) -> int:
        with self._changed:
            return self._version

    def wait(self, version: int, timeout: float) -> None:
        """Return once something was appended after version was read, or after timeout."""
        with self._changed:
            self._changed.wait_for(lambda: self._version != version, timeout)
