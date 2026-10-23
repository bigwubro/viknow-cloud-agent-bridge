"""HTTP push client for the algorithm-side usage aggregation callback."""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


class UsagePushClient:
    """POST aggregated usage to the application callback URL.

    ``token`` (optional) is sent as a ``Bearer`` authorization header. Push
    returns True only for 2xx responses; every failure is logged and reported
    as False so the worker can retry on later cycles.
    """

    def __init__(
        self,
        *,
        callback_url: str,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._callback_url = callback_url
        self._token = token
        self._timeout_seconds = timeout_seconds

    async def push(self, payload: dict[str, Any]) -> bool:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._callback_url,
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.warning("[usage] push request failed: {}", exc)
            return False
        if 200 <= response.status_code < 300:
            return True
        logger.warning(
            "[usage] push rejected by application: status={} body={}",
            response.status_code,
            _preview(response.text),
        )
        return False


def _preview(text: str, *, limit: int = 2000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
