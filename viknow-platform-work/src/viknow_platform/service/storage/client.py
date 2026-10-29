"""HTTP client for viknow-storage. Callers use library_id and asset_id only."""

from __future__ import annotations

from typing import Any

import httpx


class StorageClientError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


class StorageClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=120.0,
        )

    def close(self) -> None:
        self._http.close()

    def create_library(self, library_id: str, title: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"library_id": library_id}
        if title is not None:
            body["title"] = title
        return self._json("POST", "/libraries", json=body)

    def list_libraries(self, *, limit: int | None = None, after: str | None = None) -> list[dict[str, Any]]:
        return self._json("GET", "/libraries", params=_page(limit, after))

    def get_library(self, library_id: str) -> dict[str, Any]:
        return self._json("GET", f"/libraries/{library_id}")

    def upload_asset(
        self,
        library_id: str,
        filename: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        asset_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {}
        if asset_id is not None:
            data["asset_id"] = asset_id
        if title is not None:
            data["title"] = title
        return self._json(
            "POST",
            f"/libraries/{library_id}/assets",
            data=data,
            files={"file": (filename, content, content_type)},
        )

    def list_assets(
        self, library_id: str, *, limit: int | None = None, after: str | None = None
    ) -> list[dict[str, Any]]:
        return self._json("GET", f"/libraries/{library_id}/assets", params=_page(limit, after))

    def get_asset(self, library_id: str, asset_id: str) -> dict[str, Any]:
        return self._json("GET", f"/libraries/{library_id}/assets/{asset_id}")

    def asset_exists(self, library_id: str, asset_id: str) -> bool:
        try:
            response = self._http.head(f"/libraries/{library_id}/assets/{asset_id}")
        except httpx.HTTPError as exc:
            raise StorageClientError(503, "viknow-storage is unreachable") from exc
        if response.status_code == 404:
            return False
        self._raise(response)
        return True

    def download(self, library_id: str, asset_id: str) -> bytes:
        try:
            response = self._http.get(f"/libraries/{library_id}/assets/{asset_id}/content")
        except httpx.HTTPError as exc:
            raise StorageClientError(503, "viknow-storage is unreachable") from exc
        self._raise(response)
        return response.content

    def preview_url(self, library_id: str, asset_id: str) -> str:
        return f"{self._http.base_url}/libraries/{library_id}/assets/{asset_id}/preview"

    def content_url(self, library_id: str, asset_id: str) -> str:
        return f"{self._http.base_url}/libraries/{library_id}/assets/{asset_id}/content"

    def mint_download_url(
        self,
        library_id: str,
        asset_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] | None = None
        if ttl_seconds is not None:
            body = {"ttl_seconds": int(ttl_seconds)}
        return self._json(
            "POST",
            f"/libraries/{library_id}/assets/{asset_id}/download-url",
            json=body,
        )

    def list_artifacts(self, library_id: str, asset_id: str) -> list[dict[str, Any]]:
        return self._json("GET", f"/libraries/{library_id}/assets/{asset_id}/artifacts")

    def upload_artifact(
        self,
        library_id: str,
        asset_id: str,
        *,
        artifact_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/libraries/{library_id}/assets/{asset_id}/artifacts",
            data={"artifact_id": artifact_id},
            files={"file": (filename, content, content_type)},
        )

    def download_artifact(self, library_id: str, asset_id: str, artifact_id: str) -> bytes:
        try:
            response = self._http.get(
                f"/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}/content"
            )
        except httpx.HTTPError as exc:
            raise StorageClientError(503, "viknow-storage is unreachable") from exc
        self._raise(response)
        return response.content

    def delete_artifact(self, library_id: str, asset_id: str, artifact_id: str) -> None:
        try:
            response = self._http.delete(
                f"/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}"
            )
        except httpx.HTTPError as exc:
            raise StorageClientError(503, "viknow-storage is unreachable") from exc
        self._raise(response)

    def artifact_preview_url(self, library_id: str, asset_id: str, artifact_id: str) -> str:
        return (
            f"{self._http.base_url}/libraries/{library_id}/assets/{asset_id}"
            f"/artifacts/{artifact_id}/preview"
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise StorageClientError(503, "viknow-storage is unreachable") from exc
        self._raise(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        raise StorageClientError(response.status_code, detail)


def _page(limit: int | None, after: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if after:
        params["after"] = after
    return params
