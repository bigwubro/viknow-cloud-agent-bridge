"""HTTP API. Callers pass library_id and asset_id. Object keys stay here."""

from __future__ import annotations

import hmac
import time
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from viknow_storage.blobs import BlobStore, MemoryBlobStore, S3BlobStore
from viknow_storage.catalog import (
    CatalogError,
    add_asset,
    create_library,
    delete_library,
    filename_of,
    get_asset,
    get_library,
    list_assets,
    list_libraries,
    remove_asset,
)
from viknow_storage.db import make_engine, session_factory
from viknow_storage.artifacts import add_artifact, get_artifact, list_artifacts, remove_artifact
from viknow_storage.grants import (
    DEFAULT_GRANT_TTL_SECONDS,
    GrantError,
    build_grant_content_url,
    grant_secret,
    issue_download_grant,
    verify_download_grant,
)
from viknow_storage.settings import Settings


def create_app(settings: Settings | None = None, blobs: BlobStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.database_url:
        raise RuntimeError("STORAGE_DATABASE_URL must point at Postgres (or sqlite for tests)")
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    store = blobs if blobs is not None else _blobs(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.blob_backend == "s3" and blobs is None:
            _wait_bucket(store)
        yield

    app = FastAPI(title="viknow-storage", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.blobs = store

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def db() -> Session:
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    def authorized(request: Request) -> None:
        expected = app.state.settings.token
        if not expected:
            raise HTTPException(status_code=503, detail="storage token is not configured")
        header = request.headers.get("authorization") or ""
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def guard(session: Session = Depends(db), _: None = Depends(authorized)) -> Session:
        return session

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/libraries", status_code=201)
    def post_library(body: dict = Body(...), session: Session = Depends(guard)) -> dict:
        _only(body, {"library_id", "title"})
        try:
            row = create_library(session, str(body.get("library_id") or ""), body.get("title"))
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return row.public()

    @app.get("/libraries")
    def get_libraries(
        limit: int | None = None,
        after: str | None = None,
        session: Session = Depends(guard),
    ) -> list[dict]:
        return [row.public() for row in list_libraries(session, limit, after)]

    @app.get("/libraries/{library_id}")
    def one_library(library_id: str, session: Session = Depends(guard)) -> dict:
        try:
            return get_library(session, library_id).public()
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.delete("/libraries/{library_id}", status_code=204)
    def remove_library(library_id: str, session: Session = Depends(guard)) -> Response:
        try:
            delete_library(session, library_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return Response(status_code=204)

    @app.get("/libraries/{library_id}/assets")
    def get_assets(
        library_id: str,
        limit: int | None = None,
        after: str | None = None,
        session: Session = Depends(guard),
    ) -> list[dict]:
        try:
            rows = list_assets(session, library_id, limit, after)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return [row.public() for row in rows]

    @app.post("/libraries/{library_id}/assets", status_code=201)
    async def post_asset(
        library_id: str,
        file: UploadFile = File(...),
        asset_id: str | None = Form(default=None),
        title: str | None = Form(default=None),
        session: Session = Depends(guard),
    ) -> dict:
        try:
            name = filename_of(file.filename or "")
            payload = await file.read()
            if not payload:
                raise CatalogError(422, [{"loc": ["file"], "msg": "file is empty", "type": "value_error"}])
            content_type = file.content_type or "application/octet-stream"
            row = add_asset(
                session,
                library_id,
                asset_id=asset_id,
                filename=name,
                content_type=content_type,
                size_bytes=len(payload),
                title=title,
            )
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        try:
            app.state.blobs.put(row.object_key, payload, row.content_type)
        except Exception as exc:
            remove_asset(session, row.library_id, row.asset_id)
            raise HTTPException(status_code=503, detail="object storage is unavailable") from exc
        return row.public()

    @app.get("/libraries/{library_id}/assets/{asset_id}")
    def one_asset(library_id: str, asset_id: str, session: Session = Depends(guard)) -> dict:
        try:
            return get_asset(session, library_id, asset_id).public()
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.head("/libraries/{library_id}/assets/{asset_id}")
    def head_asset(library_id: str, asset_id: str, session: Session = Depends(guard)) -> Response:
        try:
            row = get_asset(session, library_id, asset_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return Response(
            status_code=200,
            headers={
                "Content-Type": row.content_type,
                "Content-Length": str(row.size_bytes),
            },
        )

    @app.post("/libraries/{library_id}/assets/{asset_id}/download-url")
    def mint_download_url(
        library_id: str,
        asset_id: str,
        body: dict | None = Body(default=None),
        session: Session = Depends(guard),
    ) -> dict:
        if body is not None:
            _only(body, {"ttl_seconds"})
        try:
            get_asset(session, library_id, asset_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        settings = app.state.settings
        try:
            secret = grant_secret(settings.grant_secret)
        except GrantError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        ttl = DEFAULT_GRANT_TTL_SECONDS
        if isinstance(body, dict) and body.get("ttl_seconds") is not None:
            try:
                ttl = int(body["ttl_seconds"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="ttl_seconds must be an integer") from exc
        ttl = min(max(ttl, 1), settings.grant_ttl_seconds)
        try:
            token, exp = issue_download_grant(
                library_id=library_id,
                asset_id=asset_id,
                ttl_seconds=ttl,
                secret=secret,
            )
            url = build_grant_content_url(settings.download_base_url, library_id, asset_id, token)
        except GrantError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        from datetime import datetime, timezone

        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "url": url,
            "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
            "ttl_seconds": ttl,
        }

    @app.get("/libraries/{library_id}/assets/{asset_id}/content")
    def download_asset(
        library_id: str,
        asset_id: str,
        request: Request,
        grant: str | None = None,
        range_header: str | None = Header(default=None, alias="range"),
        session: Session = Depends(db),
    ) -> Response:
        if grant:
            _authorize_grant(app.state.settings, grant, library_id, asset_id)
        else:
            authorized(request)
        return _send(app.state.blobs, session, library_id, asset_id, range_header, inline=False)

    @app.get("/libraries/{library_id}/assets/{asset_id}/preview")
    def preview_asset(
        library_id: str,
        asset_id: str,
        range_header: str | None = Header(default=None, alias="range"),
        session: Session = Depends(guard),
    ) -> Response:
        return _send(app.state.blobs, session, library_id, asset_id, range_header, inline=True)

    @app.delete("/libraries/{library_id}/assets/{asset_id}", status_code=204)
    def delete_asset(library_id: str, asset_id: str, session: Session = Depends(guard)) -> Response:
        try:
            row = get_asset(session, library_id, asset_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        try:
            app.state.blobs.delete(row.object_key)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="object storage is unavailable") from exc
        remove_asset(session, library_id, asset_id)
        return Response(status_code=204)

    @app.get("/libraries/{library_id}/assets/{asset_id}/artifacts")
    def get_artifacts(library_id: str, asset_id: str, session: Session = Depends(guard)) -> list[dict]:
        try:
            rows = list_artifacts(session, library_id, asset_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return [row.public() for row in rows]

    @app.post("/libraries/{library_id}/assets/{asset_id}/artifacts", status_code=201)
    async def post_artifact(
        library_id: str,
        asset_id: str,
        file: UploadFile = File(...),
        artifact_id: str = Form(...),
        session: Session = Depends(guard),
    ) -> dict:
        try:
            name = filename_of(file.filename or "")
            payload = await file.read()
            if not payload:
                raise CatalogError(422, [{"loc": ["file"], "msg": "file is empty", "type": "value_error"}])
            content_type = file.content_type or "application/octet-stream"
            row = add_artifact(
                session,
                library_id,
                asset_id,
                artifact_id=artifact_id,
                filename=name,
                content_type=content_type,
                size_bytes=len(payload),
            )
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        try:
            app.state.blobs.put(row.object_key, payload, row.content_type)
        except Exception as exc:
            remove_artifact(session, library_id, asset_id, row.artifact_id)
            raise HTTPException(status_code=503, detail="object storage is unavailable") from exc
        return row.public()

    @app.get("/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}")
    def one_artifact(
        library_id: str,
        asset_id: str,
        artifact_id: str,
        session: Session = Depends(guard),
    ) -> dict:
        try:
            return get_artifact(session, library_id, asset_id, artifact_id).public()
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.head("/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}")
    def head_artifact(
        library_id: str,
        asset_id: str,
        artifact_id: str,
        session: Session = Depends(guard),
    ) -> Response:
        try:
            row = get_artifact(session, library_id, asset_id, artifact_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return Response(
            status_code=200,
            headers={
                "Content-Type": row.content_type,
                "Content-Length": str(row.size_bytes),
            },
        )

    @app.get("/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}/content")
    def download_artifact(
        library_id: str,
        asset_id: str,
        artifact_id: str,
        range_header: str | None = Header(default=None, alias="range"),
        session: Session = Depends(guard),
    ) -> Response:
        try:
            row = get_artifact(session, library_id, asset_id, artifact_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return _send_bytes(
            app.state.blobs,
            row.object_key,
            row.content_type,
            row.size_bytes,
            row.filename or artifact_id,
            range_header,
            inline=False,
        )

    @app.get("/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}/preview")
    def preview_artifact(
        library_id: str,
        asset_id: str,
        artifact_id: str,
        range_header: str | None = Header(default=None, alias="range"),
        session: Session = Depends(guard),
    ) -> Response:
        try:
            row = get_artifact(session, library_id, asset_id, artifact_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return _send_bytes(
            app.state.blobs,
            row.object_key,
            row.content_type,
            row.size_bytes,
            row.filename or artifact_id,
            range_header,
            inline=True,
        )

    @app.delete("/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}", status_code=204)
    def delete_artifact_row(
        library_id: str,
        asset_id: str,
        artifact_id: str,
        session: Session = Depends(guard),
    ) -> Response:
        try:
            row = get_artifact(session, library_id, asset_id, artifact_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        try:
            app.state.blobs.delete(row.object_key)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="object storage is unavailable") from exc
        remove_artifact(session, library_id, asset_id, artifact_id)
        return Response(status_code=204)

    return app


def _authorize_grant(settings: Settings, grant: str, library_id: str, asset_id: str) -> None:
    try:
        secret = grant_secret(settings.grant_secret)
        verify_download_grant(grant, library_id=library_id, asset_id=asset_id, secret=secret)
    except GrantError as exc:
        code = str(exc)
        status = 403 if code == "grant_expired" else 401
        raise HTTPException(status_code=status, detail=code) from exc


def _blobs(settings: Settings) -> BlobStore:
    if settings.blob_backend == "memory":
        return MemoryBlobStore()
    return S3BlobStore(
        settings.s3_endpoint,
        settings.s3_access_key,
        settings.s3_secret_key,
        settings.s3_bucket,
        settings.s3_region,
    )


def _wait_bucket(store: BlobStore) -> None:
    last: Exception | None = None
    for _ in range(30):
        try:
            store.ensure_bucket()
            return
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise RuntimeError("object storage bucket is not ready") from last




def _content_disposition(disposition: str, filename: str) -> str:
    name = filename or "download"
    if name.isascii():
        safe = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'{disposition}; filename="{safe}"'
    encoded = quote(name, safe="")
    return f"{disposition}; filename=\"download\"; filename*=UTF-8''{encoded}"


def _only(body: dict, allowed: set[str]) -> None:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="expected JSON object")
    unknown = [key for key in body if key not in allowed]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", key], "msg": "extra fields not permitted", "type": "extra_forbidden"} for key in unknown],
        )


def _send(blobs: BlobStore, session: Session, library_id: str, asset_id: str, range_header: str | None, *, inline: bool) -> Response:
    try:
        row = get_asset(session, library_id, asset_id)
    except CatalogError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return _send_bytes(
        blobs,
        row.object_key,
        row.content_type,
        row.size_bytes,
        row.filename or asset_id,
        range_header,
        inline=inline,
    )


def _send_bytes(
    blobs: BlobStore,
    object_key: str,
    content_type: str,
    size_bytes: int,
    filename: str,
    range_header: str | None,
    *,
    inline: bool,
) -> Response:
    try:
        start, end = _range(range_header, size_bytes)
        body = blobs.get(object_key, start, end)
    except _BadRange as exc:
        raise HTTPException(
            status_code=416,
            detail="range not satisfiable",
            headers={"Content-Range": f"bytes */{size_bytes}"},
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="object storage is unavailable") from exc
    disposition = "inline" if inline else "attachment"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(disposition, filename),
        "Content-Length": str(len(body.data)),
    }
    status = 200
    if body.partial or start is not None:
        status = 206
        headers["Content-Range"] = f"bytes {body.start}-{body.end}/{body.size}"
    return StreamingResponse(iter([body.data]), status_code=status, media_type=content_type, headers=headers)


class _BadRange(Exception):
    pass


def _range(header: str | None, size: int) -> tuple[int | None, int | None]:
    if not header:
        return None, None
    text = header.strip()
    if not text.lower().startswith("bytes="):
        raise _BadRange()
    spec = text.split("=", 1)[1].split(",", 1)[0].strip()
    if "-" not in spec:
        raise _BadRange()
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            length = int(end_s)
            if length <= 0:
                raise _BadRange()
            start = max(size - length, 0)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError as exc:
        raise _BadRange() from exc
    if size == 0 or start < 0 or start >= size or end < start:
        raise _BadRange()
    return start, min(end, size - 1)
