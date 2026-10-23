"""Self Learning Interface. Forwards to LearningClient."""

from __future__ import annotations

from typing import Any

from viknow3.application.learning.client import LEARNING


def status() -> dict[str, Any]:
    return LEARNING.status()
