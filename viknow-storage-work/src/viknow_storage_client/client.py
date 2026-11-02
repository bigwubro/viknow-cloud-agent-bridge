"""HTTP client. Callers pass library_id and asset_id, never an object key."""

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
            timeout=60.0,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> StorageClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_library(self, library_id: str, title: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"library_id": library_id}
        if title is not None:
            body["title"] = title
        return self._json("POST", "/libraries", json=body)

    def list_libraries(self, *, limit: int | None = None, after: str | None = None) -> list[dict[str, Any]]:
        return self._json("GET", "/libraries", params=_page(limit, after))

    def get_library(self, library_id: str) -> dict[str, Any]:
        return self._json("GET", f"/libraries/{library_id}")

    def delete_library(self, library_id: str) -> None:
        self._send("DELETE", f"/libraries/{library_id}")

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
        data = {}
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

    def download_via_url(self, url: str) -> bytes:
        response = self._http.get(url)
        self._raise(response)
        return response.content

    def preview(self, library_id: str, asset_id: str, *, range_header: str | None = None) -> tuple[int, bytes]:
        headers = {"Range": range_header} if range_header else None
        response = self._http.get(f"/libraries/{library_id}/assets/{asset_id}/preview", headers=headers)
        self._raise(response)
        return response.status_code, response.content

    def delete_asset(self, library_id: str, asset_id: str) -> None:
        self._send("DELETE", f"/libraries/{library_id}/assets/{asset_id}")

    def list_artifacts(self, library_id: str, asset_id: str) -> list[dict[str, Any]]:
        return self._json("GET", f"/libraries/{library_id}/assets/{asset_id}/artifacts")

    def get_artifact(self, library_id: str, asset_id: str, artifact_id: str) -> dict[str, Any]:
        return self._json("GET", f"/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}")

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
        response = self._http.get(
            f"/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}/content"
        )
        self._raise(response)
        return response.content

    def delete_artifact(self, library_id: str, asset_id: str, artifact_id: str) -> None:
        self._send("DELETE", f"/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        self._raise(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _send(self, method: str, path: str) -> None:
        response = self._http.request(method, path)
        self._raise(response)

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
