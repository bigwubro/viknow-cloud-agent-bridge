"""Write one run's ViKnow events and keep RunState in step with them."""

from __future__ import annotations

import threading
from typing import Any

from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.events.sink import EventSink
from viknow3.clock import now

TERMINAL_EVENTS = frozenset({"run_completed", "run_failed", "run_interrupted"})


class RunEvents:
    """RunState is updated before the terminal event, so readers that stop on it see final state."""

    def __init__(self, state: RunState, sink: EventSink) -> None:
        self.state = state
        self._sink = sink
        self._started = False
        self._closed = False
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        post_terminal = frozenset({"answer_review_started", "answer_review_completed", "tool_gate_blocked"})
        if self._closed and event_type not in TERMINAL_EVENTS and event_type not in post_terminal and event_type != "message_completed":
            return
        body = {"session_id": self.state.ctx.session_id, **(payload or {})}
        self._sink.append(self.state.ctx.run_id, event_type, body, now())

    def _terminal(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """The terminal event and the run's final fields are one transaction."""
        state = self.state
        body = {"session_id": state.ctx.session_id, **(payload or {})}
        finish = {
            "status": state.status,
            "output": state.output,
            "citations": state.citations,
            "token_usage": state.token_usage,
        }
        self._sink.append(state.ctx.run_id, event_type, body, state.updated_at, finish)

    def started(self) -> None:
        if self._started:
            return
        self._started = True
        ctx = self.state.ctx
        self.emit("run_started", {"library_ids": list(ctx.library_ids)})
        self.emit("user_message", {"text": ctx.message, "attachments": list(ctx.attachments)})

    def completed(self, text: str, token_usage: dict[str, int] | None) -> None:
        if not self._close():
            return
        citations = dict(self.state.citations or {})
        if text:
            self.emit("message_completed", {"text": text, "citations": citations, "token_usage": token_usage})
        self.state.output = text
        self.state.citations = citations
        self.state.token_usage = token_usage
        self._finish("completed")
        self._terminal("run_completed")

    def failed(self, message: str, code: str | None = None) -> None:
        self.started()
        if not self._close():
            return
        self._finish("failed")
        self._terminal("run_failed", {"message": message, **({"code": code} if code else {})})

    def interrupted(self, partial_text: str = "") -> None:
        self.started()
        if not self._close():
            return
        if partial_text:
            self.state.output = partial_text
        self._finish("interrupted")
        self._terminal("run_interrupted")

    def _close(self) -> bool:
        """Only the first terminal outcome counts (the orchestrator may close a run whose task is stuck)."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            return True

    def _finish(self, status: str) -> None:
        self.state.status = status
        self.state.updated_at = now()
