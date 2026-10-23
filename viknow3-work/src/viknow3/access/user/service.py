"""User Interface: create and delete."""

from __future__ import annotations

from typing import Any

from viknow3.store import STORE


class UserError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def create_user(display_name: str | None) -> dict[str, Any]:
    return STORE.create_user(display_name).public()


def get_user(user_id: str) -> dict[str, Any]:
    user = STORE.get_user(user_id)
    if user is None:
        raise UserError(404, {"detail": "user not found"})
    return user.public()


def list_users(limit: int, after: str | None) -> list[dict[str, Any]]:
    return [item.public() for item in STORE.list_users(limit, after)]


def update_user(user_id: str, display_name: str | None) -> dict[str, Any]:
    user = STORE.get_user(user_id)
    if user is None:
        raise UserError(404, {"detail": "user not found"})
    STORE.update_user(user, display_name)
    return user.public()


def delete_user(user_id: str) -> None:
    user = STORE.get_user(user_id)
    if user is None:
        raise UserError(404, {"detail": "user not found"})
    code = STORE.delete_user(user)
    if code == "session_busy":
        raise UserError(409, {"code": "session_busy", "message": "session has a running run"})


def delete_session(user_id: str, session_id: str) -> None:
    with STORE.lock:
        result = STORE.delete_session(user_id, session_id)
    if result == "missing":
        raise UserError(404, {"detail": "session not found"})
    if result == "session_busy":
        raise UserError(409, {"code": "session_busy", "message": "session has a running run"})
