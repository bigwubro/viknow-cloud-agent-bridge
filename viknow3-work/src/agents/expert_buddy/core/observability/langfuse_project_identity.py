"""Resolve the Langfuse project associated with the service's configured API key."""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from urllib.parse import urlsplit

from agents.expert_buddy.core.config import LangfuseRunBindingConfig

ProjectFetch = Callable[[str, str, str], dict]


def _fetch_projects(host: str, public_key: str, secret_key: str) -> dict:
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    request = urllib.request.Request(
        f"{host}/api/public/projects",
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


class LangfuseProjectIdentity:
    """Read-only, retryable project identity; never guesses from environment names."""

    def __init__(
        self,
        *,
        host: str,
        public_key: str,
        secret_key: str,
        fetch: ProjectFetch = _fetch_projects,
    ) -> None:
        parsed = urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Langfuse host must be an HTTP(S) URL")
        self.host = host.rstrip("/")
        self._public_key = public_key
        self._secret_key = secret_key
        self._fetch = fetch
        self.project_id: str | None = None
        self.error_code: str | None = None

    def refresh(self) -> str | None:
        try:
            response = self._fetch(self.host, self._public_key, self._secret_key)
            projects = response["data"]
            if not isinstance(projects, list) or len(projects) != 1:
                raise ValueError("Langfuse key did not identify exactly one project")
            project_id = projects[0]["id"]
            if not isinstance(project_id, str) or not project_id:
                raise ValueError("Langfuse project id is missing")
        except urllib.error.HTTPError as exc:
            self.error_code = (
                "project_credentials_rejected"
                if exc.code in {401, 403}
                else "project_lookup_unavailable"
            )
            return None
        except (OSError, ValueError, KeyError, TypeError):
            self.error_code = "project_lookup_unavailable"
            return None
        self.project_id = project_id
        self.error_code = None
        return project_id

    def run_binding(self, *, service_id: str, environment: str) -> LangfuseRunBindingConfig:
        if self.project_id is None:
            return LangfuseRunBindingConfig()
        return LangfuseRunBindingConfig(
            service_id=service_id,
            environment=environment,
            source_id=self.project_id,
            project_id=self.project_id,
        )

    async def run(self, stop: asyncio.Event, *, retry_seconds: float = 30) -> None:
        while not stop.is_set() and self.project_id is None:
            await asyncio.to_thread(self.refresh)
            if self.project_id is not None:
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=retry_seconds)
