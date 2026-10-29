"""Connection URL helpers for runtime materialization."""

from __future__ import annotations

from urllib.parse import quote_plus


def redis_url_from_parts(host: str, port: int | str, username: str, password: str | None, database: int | str) -> str:
    auth = ""
    if username and password:
        auth = f"{quote_plus(username)}:{quote_plus(password)}@"
    elif password:
        auth = f":{quote_plus(password)}@"
    return f"redis://{auth}{host}:{port}/{database}"
