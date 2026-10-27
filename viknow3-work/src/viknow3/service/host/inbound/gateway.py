"""Inbound gateway. The only way a run enters, and the only way its events leave."""

from __future__ import annotations

from typing import Any

from viknow3.service.host.environment.orchestrator import Orchestrator
from viknow3.service.host.errors import AgentServiceError
from viknow3.service.host.events.sink import EventSink
from viknow3.service.host.outbound.gateway import OutboundGateway
from viknow3.service.host.registry.catalog import Registry
from viknow3.service.host.runtime.events import TERMINAL_EVENTS

STREAM_WAIT_SECONDS = 0.5


class InboundGateway:
    def __init__(self) -> None:
        self.registry = Registry()
        self.outbound = OutboundGateway()
        self.sink = EventSink()
        self.orchestrator = Orchestrator(self.sink, self.outbound)

    def profiles(self) -> list[dict[str, Any]]:
        return self.registry.profiles()

    def profile(self, agent_id: str) -> dict[str, Any] | None:
        found = self.registry.profile(agent_id)
        return None if found is None else found.public()

    def models(self) -> dict[str, Any]:
        return self.registry.models()

    def known_model(self, model: str) -> bool:
        return self.registry.known_model(model)

    def grant(self, user_id: str, library_ids: list[str]) -> None:
        self.outbound.grant(user_id, library_ids)

    def permission(self, user_id: str, library_id: str | None, asset_id: str | None) -> bool:
        return self.outbound.permission(user_id, library_id, asset_id)

    def submit(
        self,
        *,
        session_id: str,
        user_id: str,
        profile_id: str,
        message: str,
        model: str | None,
        file_ids: list[str],
        library_ids: list[str],
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        profile = self.registry.profile(profile_id)
        if profile is None:
            raise AgentServiceError(400, {"code": "unknown_profile", "message": "unknown agent profile"})
        self.outbound.grant(user_id, library_ids)
        state = self.orchestrator.start(
            profile,
            session_id=session_id,
            user_id=user_id,
            message=message,
            model=model,
            file_ids=file_ids,
            library_ids=library_ids,
            attachments=attachments,
        )
        return state.public()

    def events(self, run_id: str, after_sequence: int) -> list[dict[str, Any]]:
        """Events written so far. Does not wait for the run to finish."""
        return self.sink.read(run_id, after_sequence)

    def stream_events(self, run_id: str, after_sequence: int):
        """Yield events until the terminal one. A run this process is not executing yields what is stored."""
        cursor = after_sequence
        while True:
            state = self.orchestrator.find(run_id)
            finished = state is None or state.done.is_set()
            version = self.sink.version()
            for item in self.sink.read(run_id, cursor):
                cursor = int(item["sequence"])
                yield item
                if item["type"] in TERMINAL_EVENTS:
                    return
            if finished:
                return
            self.sink.wait(version, STREAM_WAIT_SECONDS)

    def knows(self, run_id: str) -> bool:
        return self.orchestrator.find(run_id) is not None

    def get(self, run_id: str) -> dict[str, Any]:
        return self.orchestrator.get(run_id).public()

    def interrupt(self, run_id: str) -> dict[str, Any]:
        return self.orchestrator.interrupt(run_id).public()
