"""Narrow Feishu Wiki client used by knowledge-source discovery."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from viknow.integrations.feishu.auth import FeishuTenantTokenProvider
from viknow.integrations.feishu.domain import FeishuSpace, FeishuWikiNode, FeishuWikiNodePage
from viknow.integrations.feishu.errors import FeishuApiError


class FeishuClient:
    """Tenant-authenticated OpenAPI calls for Wiki discovery and document materialization."""

    _EXPORT_POLL_ATTEMPTS = 30
    _EXPORT_POLL_INTERVAL_SECONDS = 2.0
    _NETWORK_ATTEMPTS = 2
    _NETWORK_RETRY_DELAY_SECONDS = 0.2

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: FeishuTenantTokenProvider,
        api_base_url: str,
    ) -> None:
        self._client = client
        self._token_provider = token_provider
        self._api_base_url = api_base_url.rstrip("/")

    async def get_space(self, space_id: str) -> FeishuSpace:
        data = await self._request_json(
            "GET",
            f"/wiki/v2/spaces/{quote(space_id, safe='')}",
        )
        return FeishuSpace.model_validate(data.get("space") or {})

    async def list_nodes(
        self,
        *,
        space_id: str,
        parent_node_token: str | None = None,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> FeishuWikiNodePage:
        params: dict[str, str | int] = {"page_size": page_size}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token
        if page_token:
            params["page_token"] = page_token
        data = await self._request_json(
            "GET",
            f"/wiki/v2/spaces/{quote(space_id, safe='')}/nodes",
            params=params,
        )
        return FeishuWikiNodePage.model_validate(data)

    async def get_node(self, node_token: str) -> FeishuWikiNode:
        data = await self._request_json(
            "GET",
            "/wiki/v2/spaces/get_node",
            params={"token": node_token},
        )
        return FeishuWikiNode.model_validate(data.get("node") or {})

    async def download_file(self, file_token: str) -> bytes:
        """Download one uploaded Drive file using tenant application identity."""

        return await self._request_bytes(
            "GET",
            f"/drive/v1/files/{quote(file_token, safe='')}/download",
        )

    async def export_docx(self, obj_token: str) -> bytes:
        """Export one native Feishu docx and return the generated DOCX bytes."""

        data = await self._request_json(
            "POST",
            "/drive/v1/export_tasks",
            json={
                "file_extension": "docx",
                "token": obj_token,
                "type": "docx",
            },
        )
        ticket = str(data.get("ticket") or "")
        if not ticket:
            raise FeishuApiError("飞书文档导出任务缺少 ticket")

        for attempt in range(self._EXPORT_POLL_ATTEMPTS):
            polled = await self._request_json(
                "GET",
                f"/drive/v1/export_tasks/{quote(ticket, safe='')}",
                params={"token": obj_token},
            )
            result = polled.get("result")
            result = result if isinstance(result, dict) else {}
            file_token = str(result.get("file_token") or "")
            if file_token:
                return await self._request_bytes(
                    "GET",
                    f"/drive/v1/export_tasks/file/{quote(file_token, safe='')}/download",
                )
            if result.get("job_error_msg"):
                raise FeishuApiError("飞书文档导出任务失败")
            if attempt + 1 < self._EXPORT_POLL_ATTEMPTS:
                await asyncio.sleep(self._EXPORT_POLL_INTERVAL_SECONDS)
        raise FeishuApiError("飞书文档导出任务超时", retryable=True)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._token_provider.get_token()
            response = await self._request_with_network_retry(
                method,
                path,
                params=params,
                json=json,
                token=token,
            )
            if response.status_code == 401 and attempt == 0:
                self._token_provider.invalidate()
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                raise FeishuApiError(
                    "飞书接口响应无法解析",
                    http_status=response.status_code,
                    retryable=response.status_code >= 500,
                ) from exc
            if not isinstance(payload, dict):
                raise FeishuApiError(
                    "飞书接口响应格式无效",
                    http_status=response.status_code,
                )
            code = int(payload.get("code", -1))
            if response.status_code >= 400 or code != 0:
                raise FeishuApiError(
                    "飞书知识库接口调用失败",
                    code=code,
                    http_status=response.status_code,
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise FeishuApiError("飞书接口返回缺少 data")
            return data
        raise FeishuApiError("飞书访问凭证已失效", http_status=401)

    async def _request_bytes(self, method: str, path: str) -> bytes:
        for attempt in range(2):
            token = await self._token_provider.get_token()
            response = await self._request_with_network_retry(method, path, token=token)
            if response.status_code == 401 and attempt == 0:
                self._token_provider.invalidate()
                continue
            if response.status_code >= 400:
                code: int | None = None
                try:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("code") is not None:
                        code = int(payload["code"])
                except (TypeError, ValueError):
                    pass
                raise FeishuApiError(
                    "飞书文件下载失败",
                    code=code,
                    http_status=response.status_code,
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            content = response.content
            if not content:
                raise FeishuApiError("飞书文件下载结果为空")
            return content
        raise FeishuApiError("飞书访问凭证已失效", http_status=401)

    async def _request_with_network_retry(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, str | int] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempts = self._NETWORK_ATTEMPTS if method.upper() == "GET" else 1
        last_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                return await self._client.request(
                    method,
                    f"{self._api_base_url}{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    logger.bind(
                        source_name="feishu.client",
                        operation="openapi_request",
                        method=method.upper(),
                        path=path,
                        retry_attempt=attempt + 1,
                        error_class=type(exc).__name__,
                    ).warning("Feishu OpenAPI request failed; retrying")
                    await asyncio.sleep(self._NETWORK_RETRY_DELAY_SECONDS)
        message = "飞书文件下载网络请求失败" if "/download" in path else "飞书知识库网络请求失败"
        raise FeishuApiError(message, retryable=True) from last_error
