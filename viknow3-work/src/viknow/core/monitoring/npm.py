"""Read-only Nginx Proxy Manager monitoring HTTP client."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import httpx

from viknow.core.config import NpmApiConfig
from viknow.core.monitoring.errors import MonitoringApiError

_MAX_RESPONSE_BYTES = 512 * 1024
_TOKEN_REFRESH_WINDOW = timedelta(seconds=60)
_PROXY_HOST_FIELDS = (
    "domain_names",
    "forward_scheme",
    "forward_host",
    "forward_port",
    "certificate_id",
    "enabled",
    "ssl_forced",
    "caching_enabled",
    "block_exploits",
    "allow_websocket_upgrade",
)
_CERTIFICATE_FIELDS = (
    "provider",
    "domain_names",
    "created_on",
    "modified_on",
    "expires_on",
)


class NpmClient:
    """Small synchronous client for NPM's public and authenticated read APIs."""

    def __init__(self, config: NpmApiConfig, *, client: httpx.Client | None = None) -> None:
        self._base_url = f"{config.base_url.rstrip('/')}/"
        self._identity_env = config.identity_env
        self._secret_env = config.secret_env
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    def health(self) -> dict[str, Any]:
        payload = self._request_json("GET", "")
        if not isinstance(payload, dict):
            self._raise_contract_error()
        status = self._required_string(payload, "status")
        setup = self._required_bool(payload, "setup")
        version = payload.get("version")
        if not isinstance(version, dict):
            self._raise_contract_error()
        return {
            "status": status,
            "setup": setup,
            "version": {
                field: self._required_int(version, field, minimum=0)
                for field in ("major", "minor", "revision")
            },
        }

    def proxy_hosts(self) -> list[dict[str, Any]]:
        payload = self._authenticated_get_json("nginx/proxy-hosts")
        if not isinstance(payload, list):
            self._raise_contract_error()
        hosts: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                self._raise_contract_error()
            self._validate_proxy_host(item)
            hosts.append({field: item[field] for field in _PROXY_HOST_FIELDS})
        return hosts

    def certificates(self) -> list[dict[str, Any]]:
        payload = self._authenticated_get_json("nginx/certificates")
        if not isinstance(payload, list):
            self._raise_contract_error()
        certificates: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                self._raise_contract_error()
            self._validate_certificate(item)
            certificates.append({field: item[field] for field in _CERTIFICATE_FIELDS})
        return certificates

    def _authenticated_get_json(self, path: str) -> Any:
        token = self._valid_token_or_login()
        return self._request_json(
            "GET",
            path,
            headers={"Authorization": f"Bearer {token}"},
        )

    def _valid_token_or_login(self) -> str:
        now = datetime.now(UTC)
        if (
            self._token
            and self._token_expires_at is not None
            and self._token_expires_at > now + _TOKEN_REFRESH_WINDOW
        ):
            return self._token

        identity = os.environ.get(self._identity_env)
        secret = os.environ.get(self._secret_env)
        if not identity or not secret:
            raise MonitoringApiError("NPM 凭据未配置")

        payload = self._request_json(
            "POST",
            "tokens",
            json_body={"identity": identity, "secret": secret, "scope": "user"},
        )
        if not isinstance(payload, dict):
            self._raise_auth_error()
        token = payload.get("token")
        expires = payload.get("expires")
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            self._raise_auth_error()
        expires_at = self._parse_expiry(expires)
        if expires_at <= datetime.now(UTC) + _TOKEN_REFRESH_WINDOW:
            self._raise_auth_error()

        self._token = token
        self._token_expires_at = expires_at
        return token

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                urljoin(self._base_url, path),
                headers=headers,
                json=json_body,
            )
        except httpx.TimeoutException:
            raise MonitoringApiError("NPM 请求超时") from None
        except httpx.RequestError:
            raise MonitoringApiError("NPM 请求失败") from None
        if not response.is_success:
            raise MonitoringApiError("NPM 上游请求失败")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise MonitoringApiError("NPM 响应不能超过 512 KiB")
        try:
            return response.json()
        except ValueError:
            raise MonitoringApiError("NPM 返回了无效响应") from None

    @staticmethod
    def _parse_expiry(value: str) -> datetime:
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        try:
            expires_at = datetime.fromisoformat(normalized)
        except ValueError:
            NpmClient._raise_auth_error()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            NpmClient._raise_auth_error()
        return expires_at

    @classmethod
    def _validate_proxy_host(cls, item: dict[str, Any]) -> None:
        cls._required_int(item, "id", minimum=1)
        cls._required_domain_names(item, minimum_items=1)
        if item.get("forward_scheme") not in {"http", "https"}:
            cls._raise_contract_error()
        cls._required_string(item, "forward_host")
        cls._required_int(item, "forward_port", minimum=1, maximum=65535)
        certificate_id = item.get("certificate_id")
        if not (
            (
                isinstance(certificate_id, int)
                and not isinstance(certificate_id, bool)
                and certificate_id >= 0
            )
            or certificate_id == "new"
        ):
            cls._raise_contract_error()
        for field in (
            "enabled",
            "ssl_forced",
            "caching_enabled",
            "block_exploits",
            "allow_websocket_upgrade",
        ):
            cls._required_bool(item, field)

    @classmethod
    def _validate_certificate(cls, item: dict[str, Any]) -> None:
        cls._required_int(item, "id", minimum=1)
        if item.get("provider") not in {"letsencrypt", "other"}:
            cls._raise_contract_error()
        cls._required_string(item, "nice_name", allow_empty=True)
        cls._required_domain_names(item, minimum_items=0)
        for field in ("created_on", "modified_on", "expires_on"):
            cls._required_string(item, field)

    @classmethod
    def _required_domain_names(cls, payload: dict[str, Any], *, minimum_items: int) -> list[str]:
        value = payload.get("domain_names")
        if (
            not isinstance(value, list)
            or len(value) < minimum_items
            or len(value) > 100
            or any(not isinstance(domain, str) or not domain for domain in value)
            or len(set(value)) != len(value)
        ):
            cls._raise_contract_error()
        return value

    @classmethod
    def _required_string(
        cls,
        payload: dict[str, Any],
        field: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or (not allow_empty and not value):
            cls._raise_contract_error()
        return value

    @classmethod
    def _required_bool(cls, payload: dict[str, Any], field: str) -> bool:
        value = payload.get(field)
        if not isinstance(value, bool):
            cls._raise_contract_error()
        return value

    @classmethod
    def _required_int(
        cls,
        payload: dict[str, Any],
        field: str,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        value = payload.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            cls._raise_contract_error()
        return value

    @staticmethod
    def _raise_auth_error() -> None:
        raise MonitoringApiError("NPM 登录响应无效")

    @staticmethod
    def _raise_contract_error() -> None:
        raise MonitoringApiError("NPM 返回了无效响应")
