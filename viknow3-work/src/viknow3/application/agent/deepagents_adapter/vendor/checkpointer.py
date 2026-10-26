"""Hand the shell's Postgres checkpointer to DeepAgentsRuntime."""

from __future__ import annotations

from typing import Any


class Viknow3CheckpointerProvider:
    """Same surface as viknow Sqlite/Postgres providers; does not close the shared pool."""

    async def get(self) -> Any:
        from viknow3.application.agentcore.runtime.checkpoint import checkpointer

        return await checkpointer()

    async def aclose(self) -> None:
        return
