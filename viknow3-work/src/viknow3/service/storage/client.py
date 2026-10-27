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
        response = self._http.head(f"/libraries/{library_id}/assets/{asset_id}")
        if response.status_code == 404:
            return False
        self._raise(response)
        return True

    def download(self, library_id: str, asset_id: str) -> bytes:
        response = self._http.get(f"/libraries/{library_id}/assets/{asset_id}/content")
        self._raise(response)
        return response.content

    def fetch_bytes(
        self,
        library_id: str,
        asset_id: str,
        *,
        preview: bool = True,
        range_header: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        path = (
            f"/libraries/{library_id}/assets/{asset_id}/preview"
            if preview
            else f"/libraries/{library_id}/assets/{asset_id}/content"
        )
        headers = {"Range": range_header} if range_header else None
        response = self._http.get(path, headers=headers)
        self._raise(response)
        out_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "content-type",
                "content-length",
                "content-range",
                "accept-ranges",
                "content-disposition",
            }
        }
        return response.status_code, out_headers, response.content

    def preview_url(self, library_id: str, asset_id: str) -> str:
        return f"{self._http.base_url}/libraries/{library_id}/assets/{asset_id}/preview"

    def content_url(self, library_id: str, asset_id: str) -> str:
        return f"{self._http.base_url}/libraries/{library_id}/assets/{asset_id}/content"

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
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
