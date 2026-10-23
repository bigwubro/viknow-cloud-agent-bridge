"""RunContext isolates one agent call from every other call in this process."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RunContext:
    profile_id: str
    user_id: str
    session_id: str
    run_id: str
    message: str
    model: str | None
    file_ids: list[str]
    library_ids: list[str]
    attachments: list[dict[str, Any]]


@dataclass
class RunState:
    ctx: RunContext
    status: str
    output: str | None
    citations: dict[str, Any]
    token_usage: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    loop_started: bool = False
    file_ids: list[str] = field(default_factory=list)
    task: Future | None = field(default=None, repr=False)
    events: Any = field(default=None, repr=False)
    aio_task: Any = field(default=None, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        from viknow3.clock import stamp

        return {
            "id": self.ctx.run_id,
            "session_id": self.ctx.session_id,
            "input": self.ctx.message,
            "model": self.ctx.model,
            "file_ids": list(self.ctx.file_ids),
            "status": self.status,
            "output": self.output,
            "citations": self.citations,
            "token_usage": self.token_usage,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
        }
