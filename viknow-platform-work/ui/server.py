"""ViKnow Playground UI — static pages; browser calls ViKnow API + viknow-storage via runtime-config.js."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parent / "static"
PLAYGROUND = STATIC / "playground"

app = FastAPI(title="ViKnow Playground", docs_url=None, redoc_url=None)


@app.get("/playground/assets/runtime-config.js")
def playground_runtime_config() -> PlainTextResponse:
    """Inject API base URL, viknow-storage URL/token, and optional app key for static JS."""
    api_base = os.environ.get("VIKNOW_PLAYGROUND_API_URL", "http://127.0.0.1:5218").strip().rstrip("/")
    storage_url = os.environ.get("VIKNOW_STORAGE_URL", "").strip().rstrip("/")
    storage_token = os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip()
    app_key = os.environ.get("VIKNOW_APP_KEY", os.environ.get("VIKNOW_APP_KEY", "")).strip()
    lines = [f"window.VIKNOW_API_BASE={json.dumps(api_base)};"]
    if storage_url:
        lines.append(f"window.VIKNOW_STORAGE_URL={json.dumps(storage_url)};")
    if storage_token:
        lines.append(f"window.VIKNOW_STORAGE_TOKEN={json.dumps(storage_token)};")
    if app_key:
        lines.append(f"window.VIKNOW_APP_KEY={json.dumps(app_key)};")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="application/javascript")


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
