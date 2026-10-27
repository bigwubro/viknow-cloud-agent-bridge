"""ViKnow Playground UI — static pages + user BFF + mounted /api/v1/knowledge."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ui.playground_access import mount_knowledge_api
from ui.playground_routes import router as playground_router

STATIC = Path(__file__).resolve().parent / "static"
PLAYGROUND = STATIC / "playground"

@asynccontextmanager
async def _playground_lifespan(application: FastAPI):
    from viknow3.service.hyperrag.runtime import init_hyperrag, shutdown_background_runtime

    await init_hyperrag()
    try:
        yield
    finally:
        await shutdown_background_runtime()


app = FastAPI(title="ViKnow Playground", docs_url=None, redoc_url=None, lifespan=_playground_lifespan)
mount_knowledge_api(app)
app.include_router(playground_router)
app.mount("/playground/assets", StaticFiles(directory=PLAYGROUND / "assets"), name="playground-assets")


@app.get("/playground/assets/storage-config.js")
def playground_storage_config() -> PlainTextResponse:
    """Expose viknow-storage URL/token to the Playground (dev / trusted networks only)."""

    base = os.environ.get("VIKNOW_STORAGE_URL", "").strip().rstrip("/")
    token = os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip()
    lines = [
        f'window.VIKNOW_STORAGE_URL = "{base}";',
        f'window.VIKNOW_STORAGE_TOKEN = "{token}";',
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="application/javascript")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/playground", status_code=302)


@app.get("/playground")
def playground_login_page() -> FileResponse:
    return FileResponse(PLAYGROUND / "login.html")


@app.get("/playground/workspace")
def playground_workspace_page() -> FileResponse:
    return FileResponse(PLAYGROUND / "workspace.html")


@app.get("/playground/library/{library_id}")
def playground_library_page(library_id: str) -> FileResponse:
    del library_id
    return FileResponse(PLAYGROUND / "library.html")


@app.get("/playground/agents")
def playground_agents_page() -> FileResponse:
    return FileResponse(PLAYGROUND / "agents.html")


@app.get("/playground/agent/{agent_id}")
def playground_agent_chat_page(agent_id: str) -> FileResponse:
    del agent_id
    return FileResponse(PLAYGROUND / "chat.html")


def main() -> None:
    import uvicorn

    host = os.environ.get("VIKNOW_UI_HOST", "0.0.0.0")
    port = int(os.environ.get("VIKNOW_UI_PORT", "5198"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
