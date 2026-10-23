"""OM Interface. Checks the admin credential at the route, then calls the OM client."""

from __future__ import annotations

from typing import Any

from viknow3.ops.client import OM, OmClientError


class OmError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def get_config() -> dict[str, Any]:
    return _call(OM.get_config)


def set_config(table: str, column: str, value: Any, row_id: str | None) -> dict[str, Any]:
    return _call(lambda: OM.set_config(table, column, value, row_id, "admin"))


def changes() -> dict[str, Any]:
    return _call(OM.changes)


def revert(change_id: str) -> dict[str, Any]:
    return _call(lambda: OM.revert(change_id, "admin"))


def _call(fn):
    try:
        return fn()
    except OmClientError as exc:
        raise OmError(exc.status, exc.body) from exc
