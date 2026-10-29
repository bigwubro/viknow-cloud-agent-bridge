"""OM client. Config lives in the dedicated ops Postgres."""

from __future__ import annotations

from typing import Any

from viknow_platform.ops.config.store import ConfigError, changes, revert, set_value, snapshot


class OmClientError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


class OmClient:
    def get_config(self) -> dict[str, Any]:
        return _call(snapshot)

    def set_config(self, table: str, column: str, value: Any, row_id: str | None, actor: str) -> dict[str, Any]:
        return _call(lambda: set_value(table, column, value, row_id, actor))

    def changes(self) -> dict[str, Any]:
        return _call(lambda: {"changes": changes()})

    def revert(self, change_id: str, actor: str) -> dict[str, Any]:
        return _call(lambda: revert(change_id, actor))


def _call(fn):
    try:
        return fn()
    except ConfigError as exc:
        raise OmClientError(exc.status, exc.body) from exc


OM = OmClient()
