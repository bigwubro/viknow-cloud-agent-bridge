"""Process entry. Mounts each access module and starts uvicorn."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from viknow3.ops.config.envfile import load_local_env

load_local_env()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from viknow3.access.agent.routes import router as agent_router
from viknow3.access.agent.service import AgentError
from viknow3.access.auth import AuthError
from viknow3.access.knowledge.routes import online_router, router as knowledge_router
from viknow3.access.knowledge.service import KnowledgeError
from viknow3.access.om.routes import router as om_router
from viknow3.access.om.service import OmError
from viknow3.access.user.routes import router as user_router
from viknow3.access.user.service import UserError


@asynccontextmanager
async def _lifespan(application: FastAPI):
    from viknow3.service.hyperrag.runtime import (
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
    origins = [item.strip() for item in os.environ.get("VIKNOW_CORS_ORIGINS", "").split(",") if item.strip()]
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-App-Key"],
        )

    def handle_access_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    for exc_type in (AgentError, UserError, OmError, AuthError, KnowledgeError):
        application.add_exception_handler(exc_type, handle_access_error)
    application.include_router(user_router)
    application.include_router(knowledge_router)
    application.include_router(online_router)
    application.include_router(agent_router)
    application.include_router(om_router)
    return application


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("VIKNOW_HOST", "127.0.0.1")
    port = int(os.environ.get("VIKNOW_PORT", "5175"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
