"""Lazy per-Profile Agent runtime registry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from viknow.core.runtime.domain import AgentProfile
from viknow.core.runtime.events import AgentRuntime

if TYPE_CHECKING:
    from viknow.core.profiles import AgentProfileCatalog


class AgentRuntimeRegistry:
    """Build and cache one Agent runtime (and its ResourceScheduler) per Profile.

    Scheduler quotas are therefore per Agent Profile, not per API worker.
    """

    def __init__(
        self,
        catalog: AgentProfileCatalog,
        factory: Callable[[AgentProfile], AgentRuntime],
    ) -> None:
        self.catalog = catalog
        self._factory = factory
        self._runtimes: dict[str, AgentRuntime] = {}
        self._run_bindings: dict[str, AgentRuntime] = {}
        self._locks = {profile.id: asyncio.Lock() for profile in catalog.all()}

    async def get(self, profile_id: str) -> AgentRuntime:
        """Return the cached runtime or build it once for the requested Profile."""

        profile = self.catalog.require(profile_id)
        cached = self._runtimes.get(profile.id)
        if cached is not None:
            return cached

        async with self._locks[profile.id]:
            cached = self._runtimes.get(profile.id)
            if cached is not None:
                return cached
            runtime = self._factory(profile)
            self._runtimes[profile.id] = runtime
            return runtime

    def bind_run(self, run_id: str, runtime: AgentRuntime) -> None:
        """Pin a run to the runtime that accepted it (survives profile-cache reset)."""

        self._run_bindings[run_id] = runtime

    def runtime_for_run_id(self, run_id: str) -> AgentRuntime | None:
        return self._run_bindings.get(run_id)

    def reset_profile_cache(self, catalog: AgentProfileCatalog) -> None:
        """Drop idle profile runtimes so the next ``get()`` uses the new catalog."""

        self.catalog = catalog
        self._runtimes.clear()
        self._locks = {profile.id: asyncio.Lock() for profile in catalog.all()}

    async def aclose(self) -> None:
        """Close every cached Profile runtime (e.g. checkpointer connections)."""

        runtimes = list(self._runtimes.values()) + list(self._run_bindings.values())
        seen: set[int] = set()
        for runtime in runtimes:
            marker = id(runtime)
            if marker in seen:
                continue
            seen.add(marker)
            await runtime.aclose()

    def cached_runtimes(self) -> dict[str, AgentRuntime]:
        """Return a shallow copy of already-built Profile runtimes."""

        return dict(self._runtimes)
