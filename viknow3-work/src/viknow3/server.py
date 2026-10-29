"""Process entry. Mounts each access module and starts uvicorn."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from viknow3.ops.config.envfile import load_local_env

load_local_env()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from viknow3.access.agent.routes import router as agent_router
from viknow3.access.agent.service import AgentError
from viknow3.access.auth import AuthError
from viknow3.access.knowledge.routes import router as knowledge_router
from viknow3.access.knowledge.service import KnowledgeError
from viknow3.access.learning.routes import router as learning_router
from viknow3.access.om.routes import router as om_router
from viknow3.access.om.service import OmError
from viknow3.access.rag.routes import media_router, router as rag_router
from viknow3.access.rag.service import RagError
from viknow3.access.user.routes import router as user_router
from viknow3.access.user.service import UserError


@asynccontextmanager
async def _lifespan(application: FastAPI):
    from viknow3.application.hyperrag.runtime import (
        init_hyperrag,
        shutdown_background_runtime,
    )

    from viknow3.access.agent.service import close_lost_runs

    close_lost_runs()
    await init_hyperrag()
    try:
        yield
    finally:
        await shutdown_background_runtime()


def create_app() -> FastAPI:
    application = FastAPI(title="ViKnow access layer", lifespan=_lifespan)

    def _cors_origins() -> list[str]:
        raw = (
            os.environ.get("VIKNOW_CORS_ORIGINS")
            or os.environ.get("VIKNOW_PLAYGROUND_CORS_ORIGINS")
            or ""
        )
        origins = [item.strip() for item in raw.split(",") if item.strip()]
        if origins:
            return origins
        return [
            "http://127.0.0.1:5198",
            "http://localhost:5198",
            "http://36.103.198.236:5198",
        ]

    from starlette.middleware.cors import CORSMiddleware

    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def handle_access_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    for exc_type in (AgentError, UserError, RagError, OmError, AuthError, KnowledgeError):
        application.add_exception_handler(exc_type, handle_access_error)
    application.include_router(user_router)
    application.include_router(knowledge_router)
    application.include_router(agent_router)
    application.include_router(rag_router)
    application.include_router(media_router)
    application.include_router(learning_router)
    application.include_router(om_router)
    return application


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("VIKNOW_HOST", "127.0.0.1")
    port = int(os.environ.get("VIKNOW_PORT", "5175"))
    uvicorn.run(app, host=host, port=port)
