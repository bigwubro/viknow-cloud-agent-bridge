from __future__ import annotations

import threading
from typing import Dict, Optional

from .config_loader import HyperRAGConfig
from .hyperrag import HyperRAG


_instances: Dict[str, HyperRAG] = {}
_lock = threading.RLock()


def get_instance(workspace: str = "default", config: Optional[HyperRAGConfig] = None) -> HyperRAG:
    """进程内获取（或创建）某个 workspace 的 HyperRAG 单活实例。

    - 若已存在，直接返回；
    - 若不存在，基于提供的 config（或默认）新建，并启动事件循环线程。
    - 禁多进程：通过 hyperrag 内部的 workspace 文件锁确保同一 workspace 单活。
    """
    key = workspace or "default"
    with _lock:
        inst = _instances.get(key)
        if inst is not None:
            return inst

        cfg = config or HyperRAGConfig(workspace=key)
        rag = HyperRAG(cfg)
        rag.start()
        _instances[key] = rag
        return rag


def stop_instance(workspace: str = "default") -> None:
    key = workspace or "default"
    with _lock:
        inst = _instances.pop(key, None)
        if inst is not None:
            inst.stop()


