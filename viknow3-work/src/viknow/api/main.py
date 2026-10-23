"""ASGI entrypoint for ViKnow API server."""

from __future__ import annotations

from viknow.api.app import create_app
from viknow.core.env import validate_deploy_env

validate_deploy_env()

app = create_app()
