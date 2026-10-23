"""Application-identity token lifecycle for the Feishu connector."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic

import httpx
from loguru import logger

from viknow.integrations.feishu.errors import FeishuApiError


class FeishuTenantTokenProvider:
    """Cache and refresh a tenant access token without exposing credentials."""

    _NETWORK_ATTEMPTS = 2
    _NETWORK_RETRY_DELAY_SECONDS = 0.2

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_base_url: str,
        app_id: str,
        app_secret: str,
        clock: Callable[[], float] = monotonic,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._client = client
        self._token_url = f"{api_base_url.rstrip('/')}/auth/v3/tenant_access_token/internal"
        self._app_id = app_id
        self._app_secret = app_secret
        self._clock = clock
        self._client_factory = client_factory
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        if self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            response = await self._request_token()
            try:
                payload = response.json()
            except ValueError as exc:
                raise FeishuApiError(
                    "飞书访问凭证响应无法解析",
                    http_status=response.status_code,
                    retryable=response.status_code >= 500,
                ) from exc
            if not isinstance(payload, dict):
                raise FeishuApiError(
                    "飞书访问凭证响应格式无效",
                    http_status=response.status_code,
                )
            code = int(payload.get("code", -1))
            token = str(payload.get("tenant_access_token") or "").strip()
            if response.status_code >= 400 or code != 0 or not token:
                raise FeishuApiError(
                    "飞书应用身份认证失败",
                    code=code,
                    http_status=response.status_code,
                    retryable=response.status_code >= 500,
                )
            expires_in = max(int(payload.get("expire", 7200)), 60)
            self._token = token
            self._expires_at = self._clock() + max(expires_in - 60, 30)
            return token

    async def _request_token(self) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(self._NETWORK_ATTEMPTS):
            try:
                if self._client_factory is None:
                    return await self._client.post(
                        self._token_url,
                        json={"app_id": self._app_id, "app_secret": self._app_secret},
                    )
                async with self._client_factory() as client:
                    return await client.post(
                        self._token_url,
                        json={"app_id": self._app_id, "app_secret": self._app_secret},
                    )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < self._NETWORK_ATTEMPTS:
                    logger.bind(
                        source_name="feishu.auth",
                        operation="refresh_tenant_access_token",
                        retry_attempt=attempt + 1,
                        error_class=type(exc).__name__,
                    ).warning("Feishu tenant token request failed; retrying with a fresh client")
                    await asyncio.sleep(self._NETWORK_RETRY_DELAY_SECONDS)
        raise FeishuApiError("飞书应用身份认证暂时不可用", retryable=True) from last_error

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0
