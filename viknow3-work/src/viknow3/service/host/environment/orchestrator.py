"""Run orchestrator. Creates a RunContext and starts the turn as a task on the agent loop."""

from __future__ import annotations

import io
import logging
import threading

from viknow3.service.host.environment.context import RunContext, RunState
from viknow3.service.host.errors import AgentServiceError
from viknow3.service.host.events.sink import EventSink
from viknow3.service.host.outbound.gateway import OutboundGateway
from viknow3.service.host.registry.catalog import Profile
from viknow3.service.host.runtime import host
from viknow3.service.host.runtime.events import RunEvents
from viknow3.service.host.runtime.loop import LOOP
from viknow3.clock import new_id, now

INTERRUPT_WAIT_SECONDS = 2.0

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, sink: EventSink, outbound: OutboundGateway) -> None:
        self._sink = sink
        self._outbound = outbound
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()

    def start(
        self,
        profile: Profile,
        *,
        session_id: str,
        user_id: str,
        message: str,
        model: str | None,
        file_ids: list[str],
        library_ids: list[str],
        attachments: list[dict],
    ) -> RunState:
        moment = now()
        ctx = RunContext(
            profile_id=profile.id,
            user_id=user_id,
            session_id=session_id,
            run_id=new_id(),
            message=message,
            model=model,
            file_ids=list(file_ids),
            library_ids=list(library_ids),
            attachments=list(attachments),
        )
        state = RunState(
            ctx=ctx,
            status="running",
            output=None,
            citations={},
            token_usage=None,
            created_at=moment,
            updated_at=moment,
            loop_started=True,
        )
        state.events = RunEvents(state, self._sink)
        with self._lock:
            self._runs[ctx.run_id] = state
        state.task = LOOP.submit(host.run(state, profile, self._sink, self._outbound))
        return state

    def find(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def interrupt(self, run_id: str) -> RunState:
        state = self._require(run_id)
        if state.status != "running":
            raise AgentServiceError(409, {"code": "run_not_running", "message": "run is not running"})
        if state.task is not None:
            state.task.cancel()
        if not state.done.wait(INTERRUPT_WAIT_SECONDS):
            log.warning("run %s did not stop %.1fs after cancel:\n%s", run_id, INTERRUPT_WAIT_SECONDS, _stack(state))
            state.events.interrupted(state.output or "")
        return state

    def get(self, run_id: str) -> RunState:
        return self._require(run_id)

    def _require(self, run_id: str) -> RunState:
        state = self._runs.get(run_id)
        if state is None:
            raise AgentServiceError(404, {"detail": "run not found"})
        return state


def _stack(state: RunState) -> str:
    task = state.aio_task
    if task is None:
        return "(task never started)"
    out = io.StringIO()
    task.print_stack(file=out)
    return out.getvalue()
