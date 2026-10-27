"""App keys for the business modules, and a separate admin credential for OM."""

from __future__ import annotations

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

APP_KEYS = {DEV_APP_KEY.key: DEV_APP_KEY}


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


def require_app(request: Request) -> AppKey:
    principal = business_principal(request.headers.get("authorization"))
    if principal is None or principal.app_key is None:
        raise AuthError(401, {"code": "invalid_app_key", "message": "app key is missing, invalid, or revoked"})
    return principal.app_key


def require_admin(request: Request) -> None:
    if admin_principal(request.headers.get("authorization")) is None:
        raise AuthError(401, {"code": "invalid_admin_key", "message": "admin credential is required"})
