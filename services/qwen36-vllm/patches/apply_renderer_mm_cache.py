#!/usr/bin/env python3
"""Minimal 0.26 patch so --renderer-num-workers > 1 can run with SHM MM cache.

Full upstream PRs #44786 / #44787 do not apply to this image (unmerged, need
rebase, 0.26 still has the hard guard). This only:

1. Lifts the ValueError in vllm.config.model.ModelConfig
2. Serializes ShmObjectStoreSenderCache mutations with an RLock

It does NOT add #44786 single-flight / cache-hit executor bypass, or #44787
Qwen3-VL compact image patches.
"""

from __future__ import annotations

from pathlib import Path

MODEL_PY = Path("/usr/local/lib/python3.12/dist-packages/vllm/config/model.py")
CACHE_PY = Path("/usr/local/lib/python3.12/dist-packages/vllm/multimodal/cache.py")

GUARD_OLD = '''            if (
                self.renderer_num_workers > 1
                and self.multimodal_config.mm_processor_cache_gb > 0
            ):
                raise ValueError(
                    "Cannot use --renderer-num-workers > 1 with the "
                    "multimodal processor cache enabled. The cache is "
                    "not thread-safe and does not support concurrent "
                    "renderer workers. Please set "
                    "--renderer-num-workers 1 (the default), or "
                    "disable the cache with --mm-processor-cache-gb 0."
                )
'''

GUARD_NEW = '''            if (
                self.renderer_num_workers > 1
                and self.multimodal_config.mm_processor_cache_gb > 0
            ):
                # 44688-min: allow concurrent renderer workers with MM cache.
                # ShmObjectStoreSenderCache is serialized by an RLock.
                pass
'''

CACHE_IMPORT_OLD = """import operator
import sys
from abc import ABC, abstractmethod
"""

CACHE_IMPORT_NEW = """import operator
import sys
import threading
from abc import ABC, abstractmethod
"""

CACHE_INIT_OLD = """        # cache prompt_updates for P0 only
        self._p0_cache: dict[str, Sequence[ResolvedPromptUpdate]] = {}
"""

CACHE_INIT_NEW = """        # cache prompt_updates for P0 only
        self._p0_cache: dict[str, Sequence[ResolvedPromptUpdate]] = {}
        self._lock = threading.RLock()
"""

GET_UPDATE_OLD = """    def get_and_update_item(
        self,
        mm_item: MultiModalProcessorCacheInItem,
        mm_hash: str,
    ) -> MultiModalProcessorCacheOutItem:
        if self._shm_cache.is_cached(mm_hash):
"""

GET_UPDATE_NEW = """    def get_and_update_item(
        self,
        mm_item: MultiModalProcessorCacheInItem,
        mm_hash: str,
    ) -> MultiModalProcessorCacheOutItem:
        with self._lock:
            return self._get_and_update_item_locked(mm_item, mm_hash)

    def _get_and_update_item_locked(
        self,
        mm_item: MultiModalProcessorCacheInItem,
        mm_hash: str,
    ) -> MultiModalProcessorCacheOutItem:
        if self._shm_cache.is_cached(mm_hash):
"""

TOUCH_OLD = """    def touch_sender_cache_item(self, mm_hash: str) -> None:
        \"\"\"Touch the item in shared memory cache to prevent eviction.
        Increments writer_flag on sender side.\"\"\"
        self._shm_cache.touch(mm_hash)
"""

TOUCH_NEW = """    def touch_sender_cache_item(self, mm_hash: str) -> None:
        \"\"\"Touch the item in shared memory cache to prevent eviction.
        Increments writer_flag on sender side.\"\"\"
        with self._lock:
            self._shm_cache.touch(mm_hash)
"""


def _replace(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    if new in text and old not in text:
        print(f"already applied: {label}")
        return
    if old not in text:
        raise SystemExit(f"pattern not found for {label} in {path}")
    path.write_text(text.replace(old, new, 1))
    print(f"applied: {label}")


def main() -> None:
    _replace(MODEL_PY, GUARD_OLD, GUARD_NEW, "model.py renderer+cache guard")
    _replace(CACHE_PY, CACHE_IMPORT_OLD, CACHE_IMPORT_NEW, "cache.py threading import")
    _replace(CACHE_PY, CACHE_INIT_OLD, CACHE_INIT_NEW, "cache.py RLock")
    _replace(CACHE_PY, GET_UPDATE_OLD, GET_UPDATE_NEW, "cache.py locked get_and_update")
    _replace(CACHE_PY, TOUCH_OLD, TOUCH_NEW, "cache.py locked touch")
    # clear_cache appears on multiple cache classes; only patch the SHM sender
    # occurrence that sits next to make_stats using _stat.
    cache_text = CACHE_PY.read_text()
    sender_clear = (
        "    @override\n"
        "    def clear_cache(self) -> None:\n"
        "        self._shm_cache.clear()\n"
        "        self._p0_cache.clear()\n"
        "\n"
        "        self._hits = 0\n"
        "        self._total = 0\n"
        "        self._last_info = CacheInfo(hits=0, total=0)\n"
        "\n"
        "    @override\n"
        "    def make_stats(self, *, delta: bool = False) -> CacheInfo:\n"
        "        return self._stat(delta=delta)\n"
    )
    sender_clear_new = (
        "    @override\n"
        "    def clear_cache(self) -> None:\n"
        "        with self._lock:\n"
        "            self._shm_cache.clear()\n"
        "            self._p0_cache.clear()\n"
        "\n"
        "            self._hits = 0\n"
        "            self._total = 0\n"
        "            self._last_info = CacheInfo(hits=0, total=0)\n"
        "\n"
        "    @override\n"
        "    def make_stats(self, *, delta: bool = False) -> CacheInfo:\n"
        "        return self._stat(delta=delta)\n"
    )
    if sender_clear_new in cache_text:
        print("already applied: cache.py locked clear")
    elif sender_clear not in cache_text:
        raise SystemExit("SHM sender clear_cache block not found")
    else:
        CACHE_PY.write_text(cache_text.replace(sender_clear, sender_clear_new, 1))
        print("applied: cache.py locked clear")
    print("ok")


if __name__ == "__main__":
    main()
