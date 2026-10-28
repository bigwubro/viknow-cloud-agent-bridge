"""App keys for the business modules, and a separate admin credential for OM."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from fastapi import Request


@dataclass(frozen=True)
class AppKey:
    key: str
    default_agent_id: str


@dataclass(frozen=True)
class Principal:
    kind: str
    app_key: AppKey | None = None


DEV_APP_KEY = AppKey("dev-app-key", "expertbuddy-deepagents")
ADMIN_KEY = "dev-admin-key"

APP_KEYS: dict[str, AppKey] = {DEV_APP_KEY.key: DEV_APP_KEY}


def register_app_key(raw: str, *, default_agent_id: str | None = None) -> None:
    key = str(raw or "").strip()
    if not key:
        return
    APP_KEYS[key] = AppKey(key, default_agent_id or DEV_APP_KEY.default_agent_id)


def _load_env_app_keys() -> None:
    for env_name in ("VIKNOW3_APP_KEY", "VIKNOW_APP_KEY"):
        register_app_key(os.environ.get(env_name, ""))


_load_env_app_keys()


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def business_principal(header: str | None) -> Principal | None:
    token = bearer(header)
    app_key = APP_KEYS.get(token or "")
    if app_key is None:
        return None
    return Principal("app", app_key)


def admin_principal(header: str | None) -> Principal | None:
    token = bearer(header)
    if token != ADMIN_KEY:
        return None
    return Principal("admin")


class AuthError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def _app_key_from_request(request: Request) -> AppKey | None:
    principal = business_principal(request.headers.get("authorization"))
    if principal is not None and principal.app_key is not None:
        return principal.app_key
    header_key = (request.headers.get("x-app-key") or "").strip()
    if header_key:
        return APP_KEYS.get(header_key)
    return None


def require_app(request: Request) -> AppKey:
    app_key = _app_key_from_request(request)
    if app_key is None:
        raise AuthError(401, {"code": "invalid_app_key", "message": "app key is missing, invalid, or revoked"})
    return app_key


def require_admin(request: Request) -> None:
    if admin_principal(request.headers.get("authorization")) is None:
        raise AuthError(401, {"code": "invalid_admin_key", "message": "admin credential is required"})
