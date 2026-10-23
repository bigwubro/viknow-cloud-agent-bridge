"""Mount access-layer API routes on Playground UI (same paths as access server)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def load_playground_env() -> None:
    from viknow3.ops.config.runtime_env import load_agent_runtime_env

    load_agent_runtime_env()


def mount_access_api(app: FastAPI) -> None:
    load_playground_env()
    from viknow3.access.agent.routes import router as agent_router
    from viknow3.access.agent.service import AgentError, close_lost_runs
    from viknow3.access.auth import APP_KEYS, AppKey, require_app
    from viknow3.access.knowledge.routes import router as knowledge_router
    from viknow3.access.knowledge.service import KnowledgeError
    from viknow3.access.rag.routes import media_router, router as rag_router
    from viknow3.access.rag.service import RagError
    from viknow3.access.user.routes import router as user_router
    from viknow3.access.user.service import UserError

    configured = os.environ.get("VIKNOW3_APP_KEY", os.environ.get("VIKNOW_APP_KEY", "dev-app-key")).strip()

    def playground_app_key() -> AppKey:
        if configured in APP_KEYS:
            return APP_KEYS[configured]
        return AppKey(configured, "deepagents")

    def knowledge_error(_request, exc: KnowledgeError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    def agent_error(_request, exc: AgentError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    def user_error(_request, exc: UserError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    def rag_error(_request, exc: RagError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    app.add_exception_handler(KnowledgeError, knowledge_error)
    app.add_exception_handler(AgentError, agent_error)
    app.add_exception_handler(UserError, user_error)
    app.add_exception_handler(RagError, rag_error)
    app.dependency_overrides[require_app] = playground_app_key
    app.include_router(user_router)
    app.include_router(knowledge_router)
    app.include_router(rag_router)
    app.include_router(media_router)
    app.include_router(agent_router)
    close_lost_runs()


def mount_knowledge_api(app: FastAPI) -> None:
    mount_access_api(app)
