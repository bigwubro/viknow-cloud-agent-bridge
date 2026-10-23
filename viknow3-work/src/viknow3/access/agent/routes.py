"""Agent Interface routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from viknow3.access.agent import service
from viknow3.access.agent.service import AgentError
from viknow3.access.auth import AppKey, require_app

router = APIRouter(prefix="/api/v1/agent", dependencies=[Depends(require_app)])


def _limit(value: int | None) -> int:
    if value is None:
        return 20
    return max(1, min(value, 100))


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    return parsed if isinstance(parsed, dict) else {}


async def _run_body(request: Request) -> tuple[dict[str, Any], list[tuple[str, str | None, bytes]]]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return await _json(request), []
    form = await request.form()
    fields: dict[str, Any] = {}
    lists: dict[str, list[str]] = {}
    uploads: list[tuple[str, str | None, bytes]] = []
    for key, value in form.multi_items():
        if hasattr(value, "read"):
            uploads.append((value.filename or "upload", value.content_type, await value.read()))
            continue
        if key in {"library_ids", "attachment_file_ids", "file_ids"}:
            lists.setdefault(key, []).append(str(value))
        else:
            fields[key] = value
    fields.update(lists)
    return fields, uploads


def _sse(events: list[dict[str, Any]]) -> StreamingResponse:
    chunks = [f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n" for item in events]
    return StreamingResponse(iter(["".join(chunks)]), media_type="text/event-stream")


def _sse_live(event_iter) -> StreamingResponse:
    def generate():
        for item in event_iter:
            yield f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/agents")
def list_agents(app_key: AppKey = Depends(require_app)) -> list[dict[str, Any]]:
    return service.list_agents(app_key)


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.get_profile(app_key, agent_id)


@router.post("/agents/{agent_id}/sessions")
async def start_agent_session(agent_id: str, request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    fields, uploads = await _run_body(request)
    return service.start_session(app_key, agent_id, fields, uploads)


@router.get("/agents/{agent_id}/sessions")
def list_agent_sessions(
    agent_id: str,
    user_id: str,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.list_sessions(app_key, user_id, agent_id, _limit(limit), after)


@router.get("/agents/{agent_id}/sessions/{session_id}")
def get_agent_session(
    agent_id: str,
    session_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    return service.get_session(app_key, session_id, agent_id)


@router.patch("/agents/{agent_id}/sessions/{session_id}")
async def update_agent_session(
    agent_id: str,
    session_id: str,
    request: Request,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    body = await _json(request)
    return service.update_session(app_key, session_id, agent_id, str(body.get("title") or ""))


@router.delete("/agents/{agent_id}/sessions/{session_id}", status_code=204)
def delete_agent_session(
    agent_id: str,
    session_id: str,
    user_id: str,
    app_key: AppKey = Depends(require_app),
) -> Response:
    service.delete_session(app_key, agent_id, session_id, user_id)
    return Response(status_code=204)


@router.post("/agents/{agent_id}/sessions/{session_id}/runs")
async def continue_agent_session(
    agent_id: str,
    session_id: str,
    request: Request,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    fields, uploads = await _run_body(request)
    return service.continue_session(app_key, agent_id, session_id, fields, uploads)


@router.get("/agents/{agent_id}/sessions/{session_id}/runs")
def list_agent_session_runs(
    agent_id: str,
    session_id: str,
    status: str | None = None,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.session_runs(app_key, session_id, agent_id, status, _limit(limit), after)


@router.get("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}")
def get_agent_session_run(
    agent_id: str,
    session_id: str,
    run_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    return service.assert_run_in_session(app_key, agent_id, session_id, run_id).public()


@router.delete("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}", status_code=204)
def delete_agent_session_run(
    agent_id: str,
    session_id: str,
    run_id: str,
    app_key: AppKey = Depends(require_app),
) -> Response:
    service.delete_run(app_key, agent_id, session_id, run_id)
    return Response(status_code=204)


@router.get("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/events")
def agent_session_run_events(
    agent_id: str,
    session_id: str,
    run_id: str,
    after_sequence: int = 0,
    live: bool = True,
    app_key: AppKey = Depends(require_app),
) -> StreamingResponse:
    service.assert_run_in_session(app_key, agent_id, session_id, run_id)
    if live:
        return _sse_live(service.stream_events(run_id, after_sequence, app_key))
    return _sse(service.read_events(run_id, after_sequence, app_key))


@router.get("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/history")
def agent_session_run_history(
    agent_id: str,
    session_id: str,
    run_id: str,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.run_history(app_key, agent_id, session_id, run_id)


@router.post("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/interrupt")
def interrupt_agent_session_run(
    agent_id: str,
    session_id: str,
    run_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    service.assert_run_in_session(app_key, agent_id, session_id, run_id)
    service.interrupt(run_id, app_key)
    return {"run_id": run_id, "session_id": session_id}


@router.get("/agents/{agent_id}/sessions/{session_id}/files")
def list_agent_session_files(
    agent_id: str,
    session_id: str,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.list_files(app_key, session_id, agent_id, _limit(limit), after)


@router.delete("/agents/{agent_id}/sessions/{session_id}/files/{file_id}", status_code=204)
def delete_agent_session_file(
    agent_id: str,
    session_id: str,
    file_id: str,
    app_key: AppKey = Depends(require_app),
) -> Response:
    service.get_session(app_key, session_id, agent_id)
    service.delete_file(app_key, file_id, agent_id, session_id)
    return Response(status_code=204)


@router.get("/profiles")
def list_profiles(app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.profiles_for(app_key)


@router.get("/profiles/{agent_id}")
def get_profile(agent_id: str, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.get_profile(app_key, agent_id)


@router.get("/models")
def list_models() -> dict[str, Any]:
    return service.models()


@router.get("/permissions")
def permissions(user_id: str, library_id: str | None = None, asset_id: str | None = None) -> dict[str, Any]:
    return service.permissions(user_id, library_id, asset_id)


@router.post("/runs")
async def create_run(request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    fields, uploads = await _run_body(request)
    return service.create_run(app_key, fields, uploads)


@router.get("/runs/{run_id}")
def get_run(run_id: str, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.get_run(run_id, app_key)


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    after_sequence: int = 0,
    live: bool = True,
    app_key: AppKey = Depends(require_app),
) -> StreamingResponse:
    if live:
        return _sse_live(service.stream_events(run_id, after_sequence, app_key))
    return _sse(service.read_events(run_id, after_sequence, app_key))


@router.post("/runs/{run_id}/interrupt")
def interrupt_run(run_id: str, app_key: AppKey = Depends(require_app)) -> dict[str, str]:
    service.interrupt(run_id, app_key)
    return {"status": "interrupted"}


@router.get("/sessions")
def list_sessions(
    user_id: str,
    agent_id: str | None = None,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.list_sessions(app_key, user_id, agent_id or app_key.default_agent_id, _limit(limit), after)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, agent_id: str | None = None, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.get_session(app_key, session_id, agent_id or app_key.default_agent_id)


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: Request,
    agent_id: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    body = await _json(request)
    return service.update_session(app_key, session_id, agent_id or app_key.default_agent_id, str(body.get("title") or ""))


@router.get("/sessions/{session_id}/runs")
def session_runs(
    session_id: str,
    status: str | None = None,
    agent_id: str | None = None,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.session_runs(app_key, session_id, agent_id or app_key.default_agent_id, status, _limit(limit), after)


@router.get("/sessions/{session_id}/workspace")
def session_workspace(session_id: str, agent_id: str | None = None, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.workspace_of(app_key, session_id, agent_id or app_key.default_agent_id)


@router.get("/sessions/{session_id}/files")
def session_files(
    session_id: str,
    agent_id: str | None = None,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.list_files(app_key, session_id, agent_id or app_key.default_agent_id, _limit(limit), after)


@router.post("/sessions/{session_id}/files")
async def upload_session_file(
    session_id: str,
    request: Request,
    agent_id: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    _fields, uploads = await _run_body(request)
    if not uploads:
        raise AgentError(422, {"detail": [{"loc": ["files"], "msg": "file required", "type": "missing"}]})
    name, mime, content = uploads[0]
    return service.upload_file(app_key, session_id, agent_id or app_key.default_agent_id, name, mime, content)


@router.get("/files/{file_id}")
def get_file(file_id: str, agent_id: str | None = None, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.get_file(app_key, file_id, agent_id or app_key.default_agent_id)


@router.get("/files/{file_id}/content")
def file_content(file_id: str, agent_id: str | None = None, app_key: AppKey = Depends(require_app)) -> Response:
    mime, content = service.file_bytes(app_key, file_id, agent_id or app_key.default_agent_id)
    return Response(content=content, media_type=mime, headers={"Accept-Ranges": "bytes"})


@router.delete("/files/{file_id}", status_code=204)
def delete_file(file_id: str, agent_id: str | None = None, app_key: AppKey = Depends(require_app)) -> Response:
    service.delete_file(app_key, file_id, agent_id or app_key.default_agent_id)
    return Response(status_code=204)
