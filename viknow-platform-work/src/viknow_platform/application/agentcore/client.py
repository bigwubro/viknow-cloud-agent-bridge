"""AgentClient. The access layer calls this module. Runs are agents registered on AgentOS."""

from __future__ import annotations

from typing import Any

from viknow_platform.application.agentcore.errors import AgentServiceError
from viknow_platform.application.agentcore.inbound.gateway import InboundGateway

AgentClientError = AgentServiceError


class AgentClient:
    def __init__(self) -> None:
        self._inbound = InboundGateway()

    def profiles(self) -> list[dict[str, Any]]:
        return self._inbound.profiles()

    def profile(self, agent_id: str) -> dict[str, Any] | None:
        return self._inbound.profile(agent_id)

    def models(self) -> dict[str, Any]:
        return self._inbound.models()

    def grant(self, user_id: str, library_ids: list[str]) -> None:
        self._inbound.grant(user_id, library_ids)

    def permission(self, user_id: str, library_id: str | None, asset_id: str | None) -> bool:
        return self._inbound.permission(user_id, library_id, asset_id)

    def known_model(self, model: str) -> bool:
        return self._inbound.known_model(model)

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
        return self._inbound.submit(
            session_id=session_id,
            user_id=user_id,
            profile_id=profile_id,
            message=message,
            model=model,
            file_ids=file_ids,
            library_ids=library_ids,
            attachments=attachments,
        )

    def events(self, run_id: str, after_sequence: int) -> list[dict[str, Any]]:
        return self._inbound.events(run_id, after_sequence)

    def stream_events(self, run_id: str, after_sequence: int):
        yield from self._inbound.stream_events(run_id, after_sequence)

    def get(self, run_id: str) -> dict[str, Any]:
        return self._inbound.get(run_id)

    def knows(self, run_id: str) -> bool:
        """Whether this process started the run (and so still holds its task)."""
        return self._inbound.knows(run_id)

    def interrupt(self, run_id: str) -> dict[str, Any]:
        return self._inbound.interrupt(run_id)


AGENT = AgentClient()
