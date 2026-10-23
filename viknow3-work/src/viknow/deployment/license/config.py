"""私有化部署 License 配置（开发态默认关闭）。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LicenseConfig(BaseModel):
    """部署级离线 License 配置。

    开发态（local/devcontainer/test）保持 ``enabled=false``，不读 license、不绑机。
    私有化 release 通过 yaml ``license.enabled: true`` 或环境变量
    ``VIKNOW_LICENSE_ENABLED=true`` 开启；下列路径默认值对应 compose 挂载约定，
    均可被 ``VIKNOW_LICENSE_*`` 环境变量覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    # 是否启用 License 校验；false 时 Guard 完全跳过（开发默认）
    enabled: bool = False
    # 客户挂载的离线 license 文件路径（含签名的 JSON）
    license_path: Path = Path("/secrets/license.lic")
    # 镜像内嵌的 Ed25519 验签公钥；私钥只在签发侧，永不进镜像
    public_key_path: Path = Path("/opt/viknow/share/license/public.pem")
    # 激活态持久化目录（activated_at / last_seen_at，用于防时钟回拨）
    state_dir: Path = Path("/var/lib/viknow/license-state")
    # 宿主机 machine-id 的只读挂载路径，用于计算绑机 fingerprint
    machine_id_path: Path = Path("/host/machine-id")
    # 运行中周期复检间隔（秒），防止长跑进程跨过 expires_at 仍可用
    check_interval_sec: int = Field(default=300, ge=30)
    # 允许的时钟误差（秒）；now 早于 last_seen 超过该值则判定回拨并停服
    clock_skew_sec: int = Field(default=60, ge=0)


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return int(raw.strip())


def resolve_license_config(base: LicenseConfig | None = None) -> LicenseConfig:
    """合并 yaml 中的 LicenseConfig 与环境变量覆盖（VIKNOW_LICENSE_*）。"""

    cfg = base or LicenseConfig()
    updates: dict[str, object] = {}

    enabled = _env_bool("VIKNOW_LICENSE_ENABLED")
    if enabled is not None:
        updates["enabled"] = enabled

    license_path = _env_path("VIKNOW_LICENSE_FILE")
    if license_path is not None:
        updates["license_path"] = license_path

    public_key_path = _env_path("VIKNOW_LICENSE_PUBLIC_KEY")
    if public_key_path is not None:
        updates["public_key_path"] = public_key_path

    state_dir = _env_path("VIKNOW_LICENSE_STATE_DIR")
    if state_dir is not None:
        updates["state_dir"] = state_dir

    machine_id_path = _env_path("VIKNOW_LICENSE_MACHINE_ID_PATH")
    if machine_id_path is not None:
        updates["machine_id_path"] = machine_id_path

    check_interval = _env_int("VIKNOW_LICENSE_CHECK_INTERVAL_SEC")
    if check_interval is not None:
        updates["check_interval_sec"] = check_interval

    clock_skew = _env_int("VIKNOW_LICENSE_CLOCK_SKEW_SEC")
    if clock_skew is not None:
        updates["clock_skew_sec"] = clock_skew

    return cfg.model_copy(update=updates) if updates else cfg
