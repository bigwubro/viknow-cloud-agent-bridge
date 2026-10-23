"""Process-level logging configuration for ViKnow."""

from __future__ import annotations

import logging as stdlib_logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from loguru import logger

from viknow.core.config import AppConfig

_VIKNOW_SINK_IDS: set[int] = set()
_LOGURU_DEFAULT_SINK_ID = 0
_SHORT_CONTEXT_ID_LENGTH = 8

_DEFAULT_EXTRA = {
    "request_id": "-",
    "user_id": "-",
    "session_id": "-",
    "workspace_id": "-",
    "job_id": "-",
}

_CONTEXT_ID_FIELDS = {
    "request_id": "req",
    "session_id": "sid",
    "user_id": "uid",
    "workspace_id": "ws",
    "job_id": "job",
}

_SOURCE_PREFIXES = ("viknow.", "hyperrag.", "lightrag.", "actio.", "tests.")
_SOURCE_ALIASES = {
    "uvicorn.error": "uvicorn",
}
_BRIDGED_STDLIB_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "fastapi", "starlette")
_DISABLED_STDLIB_LOGGER_NAMES = ("uvicorn.access",)

_TEXT_FORMAT = (
    "<green>{time:MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<5}</level> | "
    "<cyan>{extra[source]}</cyan><blue>{extra[context]}</blue> | "
    "<level>{message}</level>"
)


class _InterceptHandler(stdlib_logging.Handler):
    def emit(self, record: stdlib_logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.bind(source_name=record.name, source_line=record.lineno).opt(
            exception=record.exc_info,
            depth=6,
        ).log(level, record.getMessage())


def configure_logging(config: AppConfig) -> None:
    """Configure ViKnow-owned Loguru sinks without disturbing external sinks."""

    _remove_viknow_sinks()
    _remove_loguru_default_sink()
    logger.configure(patcher=_patch_record)
    sink_id = logger.add(
        sys.stderr,
        level=config.app.log_level,
        format=_TEXT_FORMAT,
        colorize=sys.stderr.isatty(),
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )
    _VIKNOW_SINK_IDS.add(sink_id)
    _configure_stdlib_logging(config.app.log_level)


@contextmanager
def logging_context(**fields: object) -> Iterator[None]:
    """Attach structured trace fields to logs emitted in the current context."""

    normalized: dict[str, str] = {}
    for key, value in fields.items():
        if value is not None:
            normalized[key] = _format_context_value(value)
    with logger.contextualize(**normalized):
        yield


def _reset_logging_for_tests() -> None:
    """Remove only sinks installed by :func:`configure_logging`."""

    _remove_viknow_sinks()


def _remove_viknow_sinks() -> None:
    for sink_id in list(_VIKNOW_SINK_IDS):
        try:
            logger.remove(sink_id)
        except ValueError:
            pass
        finally:
            _VIKNOW_SINK_IDS.discard(sink_id)


def _remove_loguru_default_sink() -> None:
    with suppress(ValueError):
        logger.remove(_LOGURU_DEFAULT_SINK_ID)


def _configure_stdlib_logging(level: str) -> None:
    intercept_handler = _InterceptHandler()
    for logger_name in _BRIDGED_STDLIB_LOGGER_NAMES:
        bridged_logger = stdlib_logging.getLogger(logger_name)
        bridged_logger.handlers = [intercept_handler]
        bridged_logger.setLevel(_stdlib_level(level))
        bridged_logger.disabled = False
        bridged_logger.propagate = False

    for logger_name in _DISABLED_STDLIB_LOGGER_NAMES:
        disabled_logger = stdlib_logging.getLogger(logger_name)
        disabled_logger.handlers = []
        disabled_logger.disabled = True
        disabled_logger.propagate = False


def _stdlib_level(level: str) -> int:
    normalized = level.strip().upper()
    value = stdlib_logging.getLevelName(normalized)
    return value if isinstance(value, int) else stdlib_logging.INFO


def _patch_record(record: dict[str, Any]) -> None:
    extra = record["extra"]
    for key, value in _DEFAULT_EXTRA.items():
        extra.setdefault(key, value)
    extra["source"] = _format_source(record)
    extra["context"] = _format_context(extra)


def _format_context_value(value: object) -> str:
    text = str(value).strip()
    return text if text else "-"


def _short_context_id(value: object) -> str:
    text = _format_context_value(value)
    if text == "-":
        return text
    return text[:_SHORT_CONTEXT_ID_LENGTH]


def _format_context(extra: dict[str, Any]) -> str:
    parts = []
    for key, label in _CONTEXT_ID_FIELDS.items():
        short_value = _short_context_id(extra[key])
        if short_value != "-":
            parts.append(f"{label}={short_value}")
    if not parts:
        return ""
    return " [" + " ".join(parts) + "]"


def _format_source(record: dict[str, Any]) -> str:
    extra = record["extra"]
    name = str(extra.get("source_name") or record["name"])
    name = _SOURCE_ALIASES.get(name, name)
    for prefix in _SOURCE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    line = extra.get("source_line") or record["line"]
    return f"{name}:{line}"
