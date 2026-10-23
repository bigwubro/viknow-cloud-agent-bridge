"""LearningClient. The self-learning service design is still open."""

from __future__ import annotations

from typing import Any


class LearningClient:
    def status(self) -> dict[str, Any]:
        return {"module": "self-learning", "status": "pending_design"}


LEARNING = LearningClient()
