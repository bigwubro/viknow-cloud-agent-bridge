"""Optional index tracing hooks (disabled in viknow-platform unless extended)."""

from __future__ import annotations


def configure_index_tracing(*, enabled: bool | None) -> None:
    return None


def install_index_tracing_hooks() -> None:
    return None


def is_index_tracing_enabled() -> bool:
    return False
