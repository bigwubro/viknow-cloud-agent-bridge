"""Persistence protocol for Feishu synchronization state."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from viknow.core.knowledge.feishu.domain import (
    FeishuSyncObject,
    FeishuSyncRun,
    FeishuSyncSource,
)


class FeishuSyncStore(Protocol):
    def try_source_run_lock(self, source_id: str) -> AbstractContextManager[bool]: ...

    def upsert_source(self, source: FeishuSyncSource) -> FeishuSyncSource: ...

    def get_source(self, source_id: str) -> FeishuSyncSource | None: ...

    def list_sources(self) -> list[FeishuSyncSource]: ...

    def list_due_sources(self, at: datetime) -> list[FeishuSyncSource]: ...

    def upsert_object(self, item: FeishuSyncObject) -> FeishuSyncObject: ...

    def get_object(self, source_id: str, node_token: str) -> FeishuSyncObject | None: ...

    def list_objects(self, source_id: str) -> list[FeishuSyncObject]: ...

    def delete_object(self, source_id: str, node_token: str) -> bool: ...

    def add_run(self, run: FeishuSyncRun) -> FeishuSyncRun: ...

    def update_run(self, run: FeishuSyncRun) -> FeishuSyncRun: ...

    def get_run(self, run_id: str) -> FeishuSyncRun | None: ...

    def list_runs(
        self, source_id: str | None = None, *, limit: int = 50
    ) -> list[FeishuSyncRun]: ...

    def list_active_runs(self) -> list[FeishuSyncRun]: ...


__all__ = ["FeishuSyncStore"]
