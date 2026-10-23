"""The host tells a run which libraries it may search."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Protocol, Sequence


class SearchGrant(Protocol):
    def allowed(self) -> bool:
        """True when this run may search at least one requested library."""

    def libraries(self) -> Sequence[str]:
        """Library ids this run may search."""


_GRANT: ContextVar[SearchGrant | None] = ContextVar("qa_expert_grant", default=None)
_USER_MESSAGE: ContextVar[str | None] = ContextVar("qa_expert_user_message", default=None)


def current_grant() -> SearchGrant | None:
    return _GRANT.get()


def bind_grant(grant: SearchGrant | None) -> Token[SearchGrant | None]:
    return _GRANT.set(grant)


def reset_grant(token: Token[SearchGrant | None]) -> None:
    _GRANT.reset(token)


def current_user_message() -> str | None:
    return _USER_MESSAGE.get()


def bind_user_message(message: str | None) -> Token[str | None]:
    return _USER_MESSAGE.set(message)


def reset_user_message(token: Token[str | None]) -> None:
    _USER_MESSAGE.reset(token)
