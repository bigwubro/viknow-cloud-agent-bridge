"""Load optional local `.env` for dev and playground."""

from __future__ import annotations

import os
from pathlib import Path

from viknow3.ops.config.runtime_env import load_agent_runtime_env


def load_local_env() -> None:
    root = Path(__file__).resolve().parents[4]
    for name in (".env", ".env.local"):
        path = root / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, _, value = text.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    load_agent_runtime_env()
