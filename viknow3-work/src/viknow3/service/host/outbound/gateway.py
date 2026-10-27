"""Outbound gateway. A profile may call a tool, and a user may touch a granted library or asset."""

from __future__ import annotations

from viknow3.service.host.registry.catalog import Profile


class OutboundGateway:
    def __init__(self) -> None:
        self._grants: dict[str, set[str]] = {}

    def grant(self, user_id: str, library_ids: list[str]) -> None:
        self._grants.setdefault(user_id, set()).update(library_ids)

    def permission(self, user_id: str, library_id: str | None, asset_id: str | None) -> bool:
        grants = self._grants.get(user_id, set())
        if library_id and library_id in grants:
            return True
        return bool(asset_id and asset_id in grants)

    def tool_allowed(self, profile: Profile, tool_name: str) -> bool:
        return tool_name in profile.tools
