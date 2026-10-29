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


DEV_APP_KEY = AppKey("dev-app-key", "deepagents")
ADMIN_KEY = "dev-admin-key"


def _app_keys() -> dict[str, AppKey]:
    keys: dict[str, AppKey] = {DEV_APP_KEY.key: DEV_APP_KEY}
    configured = os.environ.get("VIKNOW_APP_KEY", "").strip()
    if configured:
        keys.setdefault(configured, AppKey(configured, DEV_APP_KEY.default_agent_id))
    return keys


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _app_token(request: Request) -> str | None:
    token = bearer(request.headers.get("authorization"))
    if token:
        return token
    header = request.headers.get("x-app-key") or request.headers.get("X-App-Key")
    if header and header.strip():
        return header.strip()
    return None


def business_principal(request: Request) -> Principal | None:
    app_key = _app_keys().get(_app_token(request) or "")
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


def require_app(request: Request) -> AppKey:
    principal = business_principal(request)
    if principal is None or principal.app_key is None:
        raise AuthError(401, {"code": "invalid_app_key", "message": "app key is missing, invalid, or revoked"})
    return principal.app_key


def require_admin(request: Request) -> None:
    if admin_principal(request.headers.get("authorization")) is None:
        raise AuthError(401, {"code": "invalid_admin_key", "message": "admin credential is required"})
