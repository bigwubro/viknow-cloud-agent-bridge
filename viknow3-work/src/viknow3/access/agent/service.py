"""Agent Interface. Aggregates the session, then calls AgentClient."""

from __future__ import annotations

from typing import Any

from viknow3.access.auth import AppKey
from viknow3.application.agentcore.client import AGENT, AgentClientError
from viknow3.clock import new_id, now
from viknow3.store import MAX_FILE_BYTES, STORE, FileRec, RunRef, Session, Workspace


class AgentError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def list_agents(app_key: AppKey) -> list[dict[str, Any]]:
    del app_key
    return list(AGENT.profiles())


def profiles_for(app_key: AppKey) -> dict[str, Any]:
    rows = list_agents(app_key)
    return {"default_profile": app_key.default_agent_id, "profiles": rows}


def get_profile(app_key: AppKey, agent_id: str) -> dict[str, Any]:
    profile = AGENT.profile(agent_id)
    if profile is None:
        raise AgentError(404, {"detail": "agent not found"})
    return profile


def models() -> dict[str, Any]:
    return AGENT.models()


def permissions(user_id: str, library_id: str | None, asset_id: str | None) -> dict[str, Any]:
    if not library_id and not asset_id:
        raise AgentError(422, {"detail": [{"loc": ["query"], "msg": "library_id or asset_id is required", "type": "value_error"}]})
    if STORE.get_user(user_id) is None:
        raise AgentError(404, {"detail": "user not found"})
    return {
        "allowed": AGENT.permission(user_id, library_id, asset_id),
        "user_id": user_id,
        "library_id": library_id,
        "asset_id": asset_id,
    }


def create_run(app_key: AppKey, fields: dict[str, Any], uploads: list[tuple[str, str | None, bytes]]) -> dict[str, Any]:
    profile_id = str(fields.get("profile_id") or app_key.default_agent_id)
    session_id = str(fields.get("session_id") or "") or None
    body = _submit(app_key, profile_id, fields, uploads, session_id)
    return {
        "run_id": body["run_id"],
        "session_id": body["session_id"],
        "workspace_id": body["workspace_id"],
        "profile_id": profile_id,
        "status": body["status"],
        "file_ids": body["file_ids"],
    }


def start_session(app_key: AppKey, agent_id: str, fields: dict[str, Any], uploads: list[tuple[str, str | None, bytes]]) -> dict[str, Any]:
    """POST .../agents/{agent_id}/sessions: a new session and its first run."""
    return _submit_result(_submit(app_key, agent_id, fields, uploads, None))


def continue_session(
    app_key: AppKey, agent_id: str, session_id: str, fields: dict[str, Any], uploads: list[tuple[str, str | None, bytes]]
) -> dict[str, Any]:
    """POST .../sessions/{session_id}/runs: library_ids stay as the first run set them."""
    return _submit_result(_submit(app_key, agent_id, fields, uploads, session_id))


def _submit_result(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": body["run_id"],
        "session_id": body["session_id"],
        "workspace_id": body["workspace_id"],
        "agent_id": body["agent_id"],
        "uploaded_file_ids": body["uploaded_file_ids"],
    }


def _submit(
    app_key: AppKey,
    profile_id: str,
    fields: dict[str, Any],
    uploads: list[tuple[str, str | None, bytes]],
    session_id: str | None,
) -> dict[str, Any]:
    message = str(fields.get("message") or "")
    if not message.strip():
        raise AgentError(422, {"detail": [{"loc": ["body", "message"], "msg": "field required", "type": "missing"}]})
    user_id = str(fields.get("user_id") or "")
    if STORE.get_user(user_id) is None:
        raise AgentError(404, {"detail": "user not found"})

    profile = AGENT.profile(profile_id)
    if profile is None:
        raise AgentError(400, {"code": "unknown_profile", "message": "unknown agent profile"})
    model = fields.get("agent_model") or fields.get("model")
    if model:
        model = str(model)
        if not AGENT.known_model(model):
            raise AgentError(400, {"code": "unknown_model", "message": "unknown model"})
    else:
        model = profile.get("default_model")

    library_ids = [] if session_id else _first_run_libraries(profile_id, fields.get("library_ids"))
    file_ids = [str(item) for item in fields.get("attachment_file_ids") or fields.get("file_ids") or []]

    with STORE.lock:
        session = None
        if session_id:
            session = STORE.find_session(session_id, user_id, profile_id)
            if session is None:
                raise AgentError(404, {"detail": "session not found"})
            if STORE._session_busy(session.id):
                raise AgentError(409, {"code": "session_busy", "message": "session has a running run"})
        workspace_id = session.workspace_id if session else None
        foreign = [
            item
            for item in file_ids
            if item not in STORE.files or STORE.files[item].workspace_id != workspace_id or STORE.files[item].deleted
        ]
        if foreign:
            raise AgentError(400, {"code": "invalid_file_ids", "message": "attachment_file_ids must belong to the session", "file_ids": foreign})
        if any(len(content) > MAX_FILE_BYTES for _name, _mime, content in uploads):
            raise AgentError(400, {"code": "file_too_large", "message": "file is too large", "max_file_mb": MAX_FILE_BYTES // (1024 * 1024)})
        with STORE.atomic():
            body = _submit_rows(session, user_id, profile_id, message, model, library_ids, file_ids, uploads)
    return body


def _submit_rows(
    session: Session | None,
    user_id: str,
    profile_id: str,
    message: str,
    model: str | None,
    library_ids: list[str],
    file_ids: list[str],
    uploads: list[tuple[str, str | None, bytes]],
) -> dict[str, Any]:
    if session is None:
        session = _new_session(user_id, profile_id, message, library_ids)

    workspace = STORE.workspaces[session.workspace_id]
    uploaded = [_save_file(workspace, session, name, mime, content) for name, mime, content in uploads]
    file_ids = [*file_ids, *[item.id for item in uploaded]]
    attachments = [
        {"file_id": record.id, "file_name": record.file_name, "size_bytes": record.size_bytes}
        for record in (STORE.files[file_id] for file_id in file_ids)
    ]
    accepted = AGENT.submit(
        session_id=session.id,
        user_id=user_id,
        profile_id=profile_id,
        message=message,
        model=model,
        file_ids=file_ids,
        library_ids=list(session.library_ids),
        attachments=attachments,
    )
    moment = now()
    STORE.runs[accepted["id"]] = RunRef(
        id=accepted["id"],
        session_id=session.id,
        user_id=user_id,
        agent_id=profile_id,
        file_ids=list(file_ids),
        status=accepted["status"],
        created_at=moment,
        input=message,
        model=model,
        updated_at=moment,
    )
    STORE.add_run_files(accepted["id"], file_ids, moment)
    session.update(updated_at=moment)
    return {
        "run_id": accepted["id"],
        "session_id": session.id,
        "workspace_id": workspace.id,
        "agent_id": profile_id,
        "status": accepted["status"],
        "file_ids": list(file_ids),
        "uploaded_file_ids": [item.id for item in uploaded],
    }


def _first_run_libraries(profile_id: str, value: Any) -> list[str]:
    library_ids = _library_ids(value)
    if profile_id in ("deepagents", "adk") and not library_ids:
        raise AgentError(
            422,
            {"detail": [{"loc": ["body", "library_ids"], "msg": "select one knowledge library", "type": "missing"}]},
        )
    if len(library_ids) > 1:
        raise AgentError(
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "library_ids"],
                        "msg": "at most one library per run in playground MVP",
                        "type": "value_error",
                    }
                ]
            },
        )
    return library_ids


def read_events(run_id: str, after_sequence: int, app_key: AppKey) -> list[dict[str, Any]]:
    with STORE.lock:
        _visible_run(run_id, app_key)
    try:
        return AGENT.events(run_id, after_sequence)
    except AgentClientError as exc:
        raise AgentError(exc.status, exc.body) from exc


def stream_events(run_id: str, after_sequence: int, app_key: AppKey):
    with STORE.lock:
        _visible_run(run_id, app_key)
    try:
        yield from AGENT.stream_events(run_id, after_sequence)
    except AgentClientError as exc:
        raise AgentError(exc.status, exc.body) from exc


def run_history(app_key: AppKey, agent_id: str, session_id: str, run_id: str) -> list[dict[str, Any]]:
    run = assert_run_in_session(app_key, agent_id, session_id, run_id)
    if run.status == "running":
        raise AgentError(409, {"detail": "run_not_finished"})
    return read_events(run_id, 0, app_key)


def get_run(run_id: str, app_key: AppKey) -> dict[str, Any]:
    with STORE.lock:
        return _visible_run(run_id, app_key).public()


def interrupt(run_id: str, app_key: AppKey) -> dict[str, Any]:
    with STORE.lock:
        run = _visible_run(run_id, app_key)
        if run.status != "running":
            raise AgentError(409, {"code": "run_not_running", "message": "run is not running"})
    try:
        AGENT.interrupt(run_id)
    except AgentClientError as exc:
        raise AgentError(exc.status, exc.body) from exc
    with STORE.lock:
        run = STORE.runs[run_id]
        return {
            "run_id": run.id,
            "session_id": run.session_id,
            "status": run.status,
        }


def delete_run(app_key: AppKey, agent_id: str, session_id: str, run_id: str) -> None:
    with STORE.lock:
        run = assert_run_in_session(app_key, agent_id, session_id, run_id)
        if STORE.delete_run(run) == "run_busy":
            raise AgentError(409, {"code": "run_busy", "message": "interrupt the run or wait for it to finish"})


def delete_session(app_key: AppKey, agent_id: str, session_id: str, user_id: str) -> None:
    _check_agent(app_key, agent_id)
    result = STORE.delete_session(user_id, session_id, agent_id)
    if result == "missing":
        raise AgentError(404, {"detail": "session not found"})
    if result == "session_busy":
        raise AgentError(409, {"code": "session_busy", "message": "session has a running run"})


def list_sessions(app_key: AppKey, user_id: str, agent_id: str, limit: int, after: str | None) -> list[dict[str, Any]]:
    _check_agent(app_key, agent_id)
    rows = [
        session
        for session in STORE.sessions.values()
        if not session.deleted and session.user_id == user_id and session.agent_id == agent_id
    ]
    rows.sort(key=lambda item: item.updated_at, reverse=True)
    if after:
        index = next((i for i, item in enumerate(rows) if item.id == after), None)
        rows = [] if index is None else rows[index + 1 :]
    return [item.public() for item in rows[:limit]]


def get_session(app_key: AppKey, session_id: str, agent_id: str) -> dict[str, Any]:
    return _session(app_key, session_id, agent_id).public()


def update_session(app_key: AppKey, session_id: str, agent_id: str, title: str) -> dict[str, Any]:
    with STORE.lock:
        session = _session(app_key, session_id, agent_id)
        session.update(title=title, updated_at=now())
        return session.public()


def session_runs(app_key: AppKey, session_id: str, agent_id: str, status: str | None, limit: int, after: str | None) -> list[dict[str, Any]]:
    get_session(app_key, session_id, agent_id)
    rows = [run for run in STORE.runs.values() if run.session_id == session_id and not run.deleted]
    if status:
        rows = [run for run in rows if run.status == status]
    rows.sort(key=lambda item: item.created_at)
    if after:
        index = next((i for i, item in enumerate(rows) if item.id == after), None)
        rows = [] if index is None else rows[index + 1 :]
    return [item.public() for item in rows[:limit]]


def workspace_of(app_key: AppKey, session_id: str, agent_id: str) -> dict[str, Any]:
    session = STORE.find_session(session_id, agent_id=agent_id)
    if session is None:
        raise AgentError(404, {"detail": "workspace not found"})
    return STORE.workspaces[session.workspace_id].public()


def upload_file(app_key: AppKey, session_id: str, agent_id: str, name: str, mime: str | None, content: bytes) -> dict[str, Any]:
    with STORE.lock:
        session = _session(app_key, session_id, agent_id)
        workspace = STORE.workspaces[session.workspace_id]
        return _save_file(workspace, session, name, mime, content).public()


def list_files(app_key: AppKey, session_id: str, agent_id: str, limit: int, after: str | None) -> list[dict[str, Any]]:
    session_body = get_session(app_key, session_id, agent_id)
    workspace_id = STORE.sessions[session_body["id"]].workspace_id
    rows = [item for item in STORE.files.values() if item.workspace_id == workspace_id and not item.deleted]
    rows.sort(key=lambda item: item.created_at, reverse=True)
    if after:
        index = next((i for i, item in enumerate(rows) if item.id == after), None)
        rows = [] if index is None else rows[index + 1 :]
    return [item.public() for item in rows[:limit]]


def get_file(app_key: AppKey, file_id: str, agent_id: str) -> dict[str, Any]:
    record = STORE.files.get(file_id)
    if record is None or record.deleted or record.agent_id != agent_id:
        raise AgentError(404, {"detail": "file not found"})
    return record.public()


def file_bytes(app_key: AppKey, file_id: str, agent_id: str) -> tuple[str, bytes]:
    record = STORE.files.get(file_id)
    if record is None or record.deleted or record.agent_id != agent_id:
        raise AgentError(404, {"detail": "file not found"})
    return record.mime_type or "application/octet-stream", record.content


def delete_file(app_key: AppKey, file_id: str, agent_id: str, session_id: str | None = None) -> None:
    with STORE.lock:
        record = STORE.files.get(file_id)
        if record is None or record.deleted or record.agent_id != agent_id:
            raise AgentError(404, {"detail": "file not found"})
        if session_id is not None and record.session_id != session_id:
            raise AgentError(404, {"detail": "file not found"})
        in_use = any(
            run.status == "running" and file_id in run.file_ids and not run.deleted
            for run in STORE.runs.values()
        )
        if in_use:
            raise AgentError(409, {"code": "file_in_use", "message": "file is in use"})
        moment = now()
        record.update(deleted_at=moment, updated_at=moment)


def _check_agent(app_key: AppKey, agent_id: str) -> None:
    del app_key, agent_id


def _visible_run(run_id: str, app_key: AppKey) -> RunRef:
    del app_key
    run = STORE.runs.get(run_id)
    if run is None or run.deleted:
        raise AgentError(404, {"detail": "run not found"})
    return run


def assert_run_in_session(app_key: AppKey, agent_id: str, session_id: str, run_id: str) -> RunRef:
    _check_agent(app_key, agent_id)
    _session(app_key, session_id, agent_id)
    run = _visible_run(run_id, app_key)
    if run.session_id != session_id or run.agent_id != agent_id:
        raise AgentError(404, {"detail": "run not found"})
    return run


def close_lost_runs() -> list[str]:
    """Runs left running by a previous process have no task here any more."""
    with STORE.lock:
        lost = [run.id for run in STORE.runs.values() if run.status == "running" and not run.deleted and not AGENT.knows(run.id)]
        return STORE.fail_runs(lost, "the agent service restarted while this run was running")


def _session(app_key: AppKey, session_id: str, agent_id: str) -> Session:
    session = STORE.find_session(session_id, agent_id=agent_id)
    if session is None:
        raise AgentError(404, {"detail": "session not found"})
    return session


def _library_ids(value: Any) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        raise AgentError(422, {"detail": [{"loc": ["body", "library_ids"], "msg": "value is not a valid list", "type": "list_type"}]})
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise AgentError(422, {"detail": [{"loc": ["body", "library_ids"], "msg": "blank item", "type": "value_error"}]})
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > 100:
        raise AgentError(422, {"detail": [{"loc": ["body", "library_ids"], "msg": "at most 100 items", "type": "value_error"}]})
    return cleaned


def _new_session(user_id: str, agent_id: str, message: str, library_ids: list[str]) -> Session:
    moment = now()
    session_id = new_id()
    workspace = Workspace(new_id(), session_id, moment, moment)
    session = Session(session_id, user_id, agent_id, message[:40], workspace.id, library_ids, moment, moment)
    STORE.workspaces[workspace.id] = workspace
    STORE.sessions[session.id] = session
    return session


def _save_file(workspace: Workspace, session: Session, name: str, mime: str | None, content: bytes) -> FileRec:
    if len(content) > MAX_FILE_BYTES:
        raise AgentError(400, {"code": "file_too_large", "message": "file is too large", "max_file_mb": MAX_FILE_BYTES // (1024 * 1024)})
    moment = now()
    record = FileRec(
        id=new_id(),
        workspace_id=workspace.id,
        session_id=session.id,
        agent_id=session.agent_id,
        file_name=name or "upload",
        mime_type=mime,
        size_bytes=len(content),
        kind="upload",
        status="ready",
        content=content,
        created_at=moment,
        updated_at=moment,
    )
    STORE.files[record.id] = record
    return record
