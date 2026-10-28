"""Actio callables installed by the shell. This module does not import actio."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

_video_parser: Any = None
_video_parser_config: Any = None
_observe: Callable[..., Any] | None = None
_normalize: Callable[[Any], Any] | None = None


def install(
    *,
    video_parser: Any,
    video_parser_config: Any,
    observe_caption_generations: Callable[..., Any] | None,
    normalize_global_context: Callable[[Any], Any] | None,
) -> None:
    global _video_parser, _video_parser_config, _observe, _normalize
    _video_parser = video_parser
    _video_parser_config = video_parser_config
    _observe = observe_caption_generations
    _normalize = normalize_global_context


def reset() -> None:
    install(
        video_parser=None,
        video_parser_config=None,
        observe_caption_generations=None,
        normalize_global_context=None,
    )


def video_parser_class() -> Any:
    return _video_parser


def video_parser_config_class() -> Any:
    return _video_parser_config


def observe_caption_generations(callback: Any) -> Any:
    if _observe is None:
        return nullcontext()
    return _observe(callback)


def normalize_global_context(value: Any) -> Any:
    if _normalize is None:
        raise RuntimeError("actio client is not installed")
    return _normalize(value)
