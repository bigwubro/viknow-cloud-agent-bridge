"""One long-lived asyncio loop on a daemon thread. Every run is a task on it.

HTTP clients (httpx inside ChatOpenAI), the checkpointer pool and LangGraph
streams all bind to this loop, so nothing is ever awaited on a loop that was
closed by another run.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class AgentLoop:
    def __init__(self, name: str = "viknow-agent-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                ready = threading.Event()
                loop = asyncio.new_event_loop()

                def serve() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                threading.Thread(target=serve, name=self._name, daemon=True).start()
                ready.wait()
                self._loop = loop
            return self._loop

    def in_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Schedule a task on the agent loop. Cancelling the future cancels the task."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine on the agent loop and wait for it from a plain thread."""
        if self.in_loop():
            coro.close()
            raise RuntimeError("AgentLoop.call would block its own loop")
        return self.submit(coro).result(timeout)


LOOP = AgentLoop()
