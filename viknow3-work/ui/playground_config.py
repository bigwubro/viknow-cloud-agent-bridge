"""Runtime config injected into Playground HTML as JavaScript."""

from __future__ import annotations

import os
from urllib.parse import urlparse


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"} or host.endswith(".localhost")


def browser_storage_url() -> str:
    """Public viknow-storage base for the browser (never the Playground UI proxy)."""

    for key in ("VIKNOW_STORAGE_BROWSER_URL", "VIKNOW_STORAGE_PUBLIC_URL"):
        value = os.environ.get(key, "").strip().rstrip("/")
        if value:
            return value
    public_host = os.environ.get("VIKNOW_STORAGE_PUBLIC_HOST", "").strip()
    public_port = os.environ.get("VIKNOW_STORAGE_PUBLIC_PORT", "").strip() or "5219"
    if public_host:
        scheme = os.environ.get("VIKNOW_STORAGE_PUBLIC_SCHEME", "http").strip() or "http"
        return f"{scheme}://{public_host}:{public_port}".rstrip("/")
    return ""


def playground_config_js() -> str:
    api = os.environ.get("VIKNOW_PLAYGROUND_API_URL", "").strip().rstrip("/")
    storage = browser_storage_url()
    token = os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip()
    app_key = os.environ.get("VIKNOW3_APP_KEY", os.environ.get("VIKNOW_APP_KEY", "")).strip()
    lines = [
        f'window.VIKNOW_API_URL = "{api}";',
        f'window.VIKNOW_STORAGE_URL = "{storage}";',
        f'window.VIKNOW_STORAGE_TOKEN = "{token}";',
    ]
    if app_key:
        lines.append(f'window.VIKNOW_APP_KEY = "{app_key}";')
    return "\n".join(lines) + "\n"
