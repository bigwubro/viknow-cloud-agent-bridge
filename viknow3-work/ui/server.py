"""ViKnow Playground UI — static pages only (integrator service).

Business APIs run on ``viknow3.server`` (``VIKNOW_PORT``). The browser calls
viknow-storage and ViKnow API directly (see ``playground-config.js``).
This process serves HTML/JS only.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ui.playground_config import playground_config_js
from ui.playground_routes import router as playground_router

STATIC = Path(__file__).resolve().parent / "static"
PLAYGROUND = STATIC / "playground"

app = FastAPI(title="ViKnow Playground", docs_url=None, redoc_url=None)
app.include_router(playground_router)


@app.get("/playground/assets/playground-config.js")
def playground_runtime_config() -> PlainTextResponse:
    return PlainTextResponse(playground_config_js(), media_type="application/javascript")


@app.get("/playground/assets/storage-config.js")
def playground_storage_config_compat() -> PlainTextResponse:
    """Deprecated alias; use playground-config.js."""

    return PlainTextResponse(playground_config_js(), media_type="application/javascript")


app.mount("/playground/assets", StaticFiles(directory=PLAYGROUND / "assets"), name="playground-assets")


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
