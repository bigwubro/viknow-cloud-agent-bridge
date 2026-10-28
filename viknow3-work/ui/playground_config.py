"""Runtime config injected into Playground HTML as JavaScript."""

from __future__ import annotations

import os


def playground_config_js() -> str:
    api = os.environ.get("VIKNOW_PLAYGROUND_API_URL", "").strip().rstrip("/")
    storage = (
        os.environ.get("VIKNOW_STORAGE_PUBLIC_URL", "").strip()
        or os.environ.get("VIKNOW_STORAGE_URL", "").strip()
    ).rstrip("/")
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
