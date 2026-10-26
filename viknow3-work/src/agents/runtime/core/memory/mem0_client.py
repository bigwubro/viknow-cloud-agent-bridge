"""Low-level mem0 OSS wrapper with MemoryScope enforcement."""

from __future__ import annotations

from typing import Any, Protocol

from agents.runtime.core.memory.domain import MemoryScope


class Mem0Client(Protocol):
    def add(
        self,
        scope: MemoryScope,
        messages: list[dict[str, str]] | str,
        *,
        infer: bool,
        metadata: dict[str, str] | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]: ...

    def search(
        self,
        scope: MemoryScope,
        query: str,
        *,
        top_k: int = 10,
        extra_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_all(
        self,
        scope: MemoryScope,
        *,
        top_k: int = 100,
        extra_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def update(self, memory_id: str, text: str) -> None: ...

    def delete(self, memory_id: str) -> None: ...


class Mem0MemoryClient:
    """mem0.Memory adapter that always applies MemoryScope."""

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    def add(
        self,
        scope: MemoryScope,
        messages: list[dict[str, str]] | str,
        *,
        infer: bool,
        metadata: dict[str, str] | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        payload_metadata = scope.write_metadata(**(metadata or {}))
        kwargs: dict[str, Any] = {
            "user_id": scope.user_id,
            "agent_id": scope.agent_id,
            "infer": infer,
            "metadata": payload_metadata,
        }
        if scope.session_id is not None:
            kwargs["run_id"] = scope.session_id
        if prompt is not None:
            kwargs["prompt"] = prompt
        result = self._memory.add(messages, **kwargs)
        return result if isinstance(result, dict) else {"results": result}

    def search(
        self,
        scope: MemoryScope,
        query: str,
        *,
        top_k: int = 10,
        extra_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        filters = scope.read_filters()
        if extra_filters:
            filters.update(extra_filters)
        response = self._memory.search(query, filters=filters, top_k=top_k)
        return _unwrap_results(response)

    def get_all(
        self,
        scope: MemoryScope,
        *,
        top_k: int = 100,
        extra_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        filters = scope.read_filters()
        if extra_filters:
            filters.update(extra_filters)
        response = self._memory.get_all(filters=filters, top_k=top_k)
        return _unwrap_results(response)

    def update(self, memory_id: str, text: str) -> None:
        self._memory.update(memory_id, data=text)

    def delete(self, memory_id: str) -> None:
        self._memory.delete(memory_id)


def build_mem0_memory_client(config: dict[str, Any]) -> Mem0MemoryClient:
    from mem0 import Memory

    memory = Memory.from_config(config)
    return Mem0MemoryClient(memory)


def _unwrap_results(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        results = response.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return []
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    return []
