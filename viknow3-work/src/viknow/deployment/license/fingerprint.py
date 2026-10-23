"""运行时绑机：从挂载的 machine-id 计算 fingerprint。

采集不走本包：交付包内独立脚本 ``docker/release-assets/collect-fingerprint.py``
（仅标准库）。此处算法须与该脚本保持一致（测试锁定）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from viknow.deployment.license.models import LicenseError, LicenseErrorCode


def read_machine_id(path: Path) -> str:
    """读取并规范化 machine-id 文本。"""

    if not path.is_file():
        raise LicenseError(
            LicenseErrorCode.MACHINE_ID_MISSING,
            f"machine-id file not found: {path}",
        )
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise LicenseError(
            LicenseErrorCode.MACHINE_ID_MISSING,
            f"machine-id file is empty: {path}",
        )
    return value


def compute_machine_fingerprint(machine_id: str) -> str:
    """对规范化 machine-id 做稳定 SHA-256 hex。"""

    normalized = machine_id.strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def fingerprint_from_path(path: Path) -> str:
    """从路径读取 machine-id 并返回 fingerprint。"""

    return compute_machine_fingerprint(read_machine_id(path))
