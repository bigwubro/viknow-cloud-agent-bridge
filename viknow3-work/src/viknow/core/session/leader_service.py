"""Redis db0 lease for startup reclaim and exclusive db0 use.

The lease key lives on Redis database 0. Acquire uses ``SET NX EX``: first
holder wins; a later db0 process fails acquire and must not start. Processes on
a non-zero Redis DB skip the lease and are never leader (no shared reclaim).

Crash without ``DEL`` is fine: the key TTL expires and another db0 process may
acquire. Renewal runs on a dedicated thread so a blocked asyncio loop cannot
expire the key. If TTL still lapses with no other holder, this process
reclaims. SIGTERM only happens when Redis shows a different holder.
"""

from __future__ import annotations

import os
import re
import signal
import threading
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger

from viknow.core.config import AppConfig

LEASE_KEY = "viknow:runtime:leader"
LEASE_TTL_SECONDS = 90
LEASE_RENEW_INTERVAL_SECONDS = 10

LeaseTouchResult = Literal["renewed", "reclaimed", "lost"]

_REDIS_DB_PATH_RE = re.compile(r"^(\d+)(?:/.*)?$")

_TOUCH_LUA = """
local current = redis.call("get", KEYS[1])
if current == ARGV[1] then
  redis.call("expire", KEYS[1], tonumber(ARGV[2]))
  return 1
end
if current == false then
  local ok = redis.call("set", KEYS[1], ARGV[1], "EX", tonumber(ARGV[2]), "NX")
  if ok then
    return 2
  end
  return 0
end
return 0
"""

_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""


@dataclass
class _LeaseState:
    holder_id: str
    redis_url: str | None = None
    client: Any | None = None
    held: bool = False
    local_only: bool = False
    stop: threading.Event = field(default_factory=threading.Event)
    renew_thread: threading.Thread | None = field(default=None, repr=False)


_lease: _LeaseState | None = None


def is_leader_service() -> bool:
    """Return True when this process currently holds the runtime leader lease."""

    return _lease is not None and _lease.held


def leader_holder_id() -> str | None:
    """Return the lease holder id when this process holds the lease."""

    if _lease is None or not _lease.held:
        return None
    return _lease.holder_id


def resolve_runtime_redis_url(config: AppConfig) -> str | None:
    """Redis URL this process will use, or None when Redis is unused."""

    redis_in_use = config.scheduler.unified.enabled or bool(os.getenv("REDIS_HOST", "").strip())
    if not redis_in_use:
        return None
    redis_url = config.scheduler.unified.redis_url.strip()
    if redis_url:
        return redis_url
    database = os.getenv("REDIS_DATABASE", "").strip() or "0"
    return f"redis://localhost/{database}"


def acquire_leader_lease(config: AppConfig) -> bool:
    """Try to become leader. Safe to call once per process lifespan.

    - No Redis configured: local-only leader (single-node / unit tests).
    - Redis DB != 0: not leader (isolated queue; skip shared reclaim).
    - Redis DB == 0: ``SET NX EX`` on :data:`LEASE_KEY`; failure means another
      holder is alive (caller must refuse startup via the db0 gate).
    """

    global _lease
    release_leader_lease()

    holder_id = str(uuid4())
    redis_url = resolve_runtime_redis_url(config)
    if redis_url is None:
        _lease = _LeaseState(holder_id=holder_id, held=True, local_only=True)
        logger.info("Leader lease: Redis unused; local-only leader (holder={})", holder_id)
        return True

    database = redis_database_from_url(redis_url)
    if database is None:
        raise RuntimeError(f"Cannot parse Redis database from URL for leader lease: {redis_url}")
    if database != 0:
        _lease = _LeaseState(holder_id=holder_id, redis_url=redis_url, held=False, local_only=False)
        logger.info(
            "Leader lease skipped: Redis DB {} is not db0 (holder would not reclaim)",
            database,
        )
        return False

    client = _connect_redis(redis_url)
    state = _LeaseState(
        holder_id=holder_id,
        redis_url=redis_url,
        client=client,
        held=False,
        local_only=False,
    )
    acquired = bool(client.set(LEASE_KEY, holder_id, nx=True, ex=LEASE_TTL_SECONDS))
    if not acquired:
        _lease = state
        logger.warning(
            "Leader lease not acquired on Redis db0 (key={}); another holder is active",
            LEASE_KEY,
        )
        return False

    state.held = True
    _lease = state
    logger.info(
        "Leader lease acquired on Redis db0 (key={}, holder={}, ttl={}s)",
        LEASE_KEY,
        holder_id,
        LEASE_TTL_SECONDS,
    )
    return True


def start_leader_lease_renewal() -> None:
    """Start a background thread that renews the Redis db0 lease."""

    if _lease is None or not _lease.held or _lease.local_only or _lease.client is None:
        return
    thread = _lease.renew_thread
    if thread is not None and thread.is_alive():
        return

    _lease.stop.clear()
    renew_thread = threading.Thread(
        target=_renew_loop,
        name="viknow-leader-lease-renew",
        daemon=True,
    )
    _lease.renew_thread = renew_thread
    renew_thread.start()


def release_leader_lease() -> None:
    """Drop the lease (compare-and-DEL) and stop renewal."""

    global _lease
    state = _lease
    _lease = None
    if state is None:
        return
    state.stop.set()
    thread = state.renew_thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=LEASE_RENEW_INTERVAL_SECONDS + 1.0)
    if state.held and not state.local_only and state.client is not None:
        try:
            state.client.eval(_RELEASE_LUA, 1, LEASE_KEY, state.holder_id)
        except Exception:
            logger.exception(
                "Failed to release Redis leader lease (holder={}); TTL will expire it",
                state.holder_id,
            )
        else:
            logger.info("Leader lease released (holder={})", state.holder_id)
    if state.client is not None:
        try:
            state.client.close()
        except Exception:
            logger.exception("Failed to close leader-lease Redis client")
    state.held = False


def redis_database_from_url(redis_url: str) -> int | None:
    """Parse the Redis DB index from a ``redis://`` / ``rediss://`` URL.

    Missing path means database 0 (Redis default). Empty URL returns None.
    """

    text = redis_url.strip()
    if not text:
        return None
    parsed = urlparse(text)
    path = (parsed.path or "").lstrip("/")
    if not path:
        return 0
    match = _REDIS_DB_PATH_RE.match(path)
    if match is None:
        return None
    return int(match.group(1))


def assert_redis_db0_allows_only_leader(*, redis_url: str) -> None:
    """Refuse startup when a non-leader process targets Redis database 0."""

    database = redis_database_from_url(redis_url)
    if database is None or database != 0:
        return
    if is_leader_service():
        return
    raise RuntimeError(
        "Redis database 0 is reserved for the process holding the "
        f"{LEASE_KEY} lease. Another holder is active (or acquire failed). "
        f"Wait up to {LEASE_TTL_SECONDS}s after it stops, or set "
        "REDIS_DATABASE / scheduler.unified.redis_url to a non-zero database."
    )


def assert_configured_redis_db0_allows_only_leader(config: AppConfig) -> None:
    """Enforce the db0 leader rule for Redis this process is configured to use."""

    if is_leader_service():
        return
    redis_url = resolve_runtime_redis_url(config)
    if redis_url is None:
        return
    assert_redis_db0_allows_only_leader(redis_url=redis_url)


def _connect_redis(redis_url: str) -> Any:
    from redis import from_url

    return from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        socket_keepalive=True,
    )


def _touch_leader_lease(state: _LeaseState) -> LeaseTouchResult:
    assert state.client is not None
    result = int(
        state.client.eval(
            _TOUCH_LUA,
            1,
            LEASE_KEY,
            state.holder_id,
            str(LEASE_TTL_SECONDS),
        )
        or 0
    )
    if result == 1:
        return "renewed"
    if result == 2:
        return "reclaimed"
    return "lost"


def _renew_loop() -> None:
    state = _lease
    if state is None:
        return
    while not state.stop.wait(LEASE_RENEW_INTERVAL_SECONDS):
        if _lease is not state or not state.held or state.client is None:
            return
        try:
            outcome = _touch_leader_lease(state)
        except Exception:
            logger.exception(
                "Leader lease renew failed on Redis db0 (holder={}); will retry",
                state.holder_id,
            )
            continue
        if outcome == "renewed":
            continue
        if outcome == "reclaimed":
            logger.warning(
                "Leader lease reclaimed after TTL expiry on Redis db0 "
                "(key={}, holder={}); no other holder was present",
                LEASE_KEY,
                state.holder_id,
            )
            continue
        state.held = False
        logger.error(
            "Leader lease lost on Redis db0 (key={}, holder={}); "
            "another db0 holder is active. Shutting down to avoid demoted dual writers. "
            "Stop the other db0 holder, or use REDIS_DATABASE != 0.",
            LEASE_KEY,
            state.holder_id,
        )
        os.kill(os.getpid(), signal.SIGTERM)
        return


__all__ = [
    "LEASE_KEY",
    "LEASE_RENEW_INTERVAL_SECONDS",
    "LEASE_TTL_SECONDS",
    "acquire_leader_lease",
    "assert_configured_redis_db0_allows_only_leader",
    "assert_redis_db0_allows_only_leader",
    "is_leader_service",
    "leader_holder_id",
    "redis_database_from_url",
    "release_leader_lease",
    "resolve_runtime_redis_url",
    "start_leader_lease_renewal",
]
