"""Restricted ASGI surface for isolated optimizer evaluation services."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from viknow.api.app import create_app
from viknow.core.config import AppConfig

type ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class EvalOnlyASGIApp:
    """Expose only the evaluation-run endpoint while preserving app lifespan."""

    def __init__(self, app: ASGIApp) -> None:
        self.inner_app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.inner_app(scope, receive, send)
            return
        if scope.get("method") == "POST" and scope.get("path") == "/api/v1/eval/runs":
            await self.inner_app(scope, receive, send)
            return
        body = json.dumps(
            {"detail": {"code": "eval_only_service", "message": "route unavailable"}},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_eval_only_app(config: AppConfig) -> EvalOnlyASGIApp:
    """Build the regular runtime behind an optimizer-safe HTTP boundary."""

    return EvalOnlyASGIApp(create_app(config))
