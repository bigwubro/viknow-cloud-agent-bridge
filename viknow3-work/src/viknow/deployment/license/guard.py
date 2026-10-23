"""License verification guard for private deployment startup and runtime."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from loguru import logger

from viknow.deployment.license.config import LicenseConfig, resolve_license_config
from viknow.deployment.license.crypto import load_and_verify_license
from viknow.deployment.license.fingerprint import fingerprint_from_path
from viknow.deployment.license.models import (
    LicenseError,
    LicenseErrorCode,
    LicensePayload,
    LicenseStatus,
)
from viknow.deployment.license.state import check_and_update_state, utc_now


class LicenseGuard:
    """私有化部署的运行时授权闸门。

    在 API 进程生命周期内决定服务是否可对外提供能力：开发态
    ``license.enabled=false`` 时整条链路旁路，不读 license、不绑机、不启周期任务；
    私有化 / release 启用后，在启动与周期复检中串行完成：

    1. 用镜像内公钥对 ``license.lic`` 做 Ed25519 验签；
    2. 用宿主机 machine-id 计算 fingerprint，与 license 绑机字段比对；
    3. 校验当前时间落在 ``issued_at``～``expires_at`` 内（到期硬停）；
    4. 更新加密激活态，抑制简单的时钟回拨绕过。

    任一环节失败即抛出 ``LicenseError``，由接入层拒绝启动或停服；本类不负责签发。
    """

    def __init__(self, config: LicenseConfig) -> None:
        self.config = resolve_license_config(config)
        self._payload: LicensePayload | None = None
        self._last_error: LicenseError | None = None
        self._revalidate_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def status(self) -> LicenseStatus:
        if not self.config.enabled:
            return LicenseStatus(enabled=False, valid=True, code=LicenseErrorCode.DISABLED)
        if self._last_error is not None:
            return LicenseStatus(
                enabled=True,
                valid=False,
                code=self._last_error.code,
                customer_id=self._payload.customer_id if self._payload else None,
                license_id=self._payload.license_id if self._payload else None,
                expires_at=self._payload.expires_at if self._payload else None,
                fingerprint_prefix=_fingerprint_prefix(
                    self._payload.machine_fingerprint if self._payload else None
                ),
            )
        if self._payload is None:
            return LicenseStatus(enabled=True, valid=False, code=LicenseErrorCode.MISSING_FILE)
        return LicenseStatus(
            enabled=True,
            valid=True,
            customer_id=self._payload.customer_id,
            license_id=self._payload.license_id,
            expires_at=self._payload.expires_at,
            fingerprint_prefix=_fingerprint_prefix(self._payload.machine_fingerprint),
        )

    def verify_startup(self, *, now: datetime | None = None) -> LicensePayload | None:
        """进程启动时的完整授权校验，供 FastAPI lifespan 在业务初始化前调用。

        - ``enabled=false``：清空内部状态并返回 ``None``，调用方继续正常启动。
        - ``enabled=true``：执行完整验签/绑机/到期/激活态校验；
          成功返回 ``LicensePayload``，失败抛出 ``LicenseError``（阻止服务起来）。

        ``now`` 仅用于测试注入当前时间；生产路径省略即可。
        """

        if not self.config.enabled:
            self._payload = None
            self._last_error = None
            return None
        try:
            payload = self._verify(now=now or utc_now())
            self._payload = payload
            self._last_error = None
            return payload
        except LicenseError as exc:
            self._last_error = exc
            logger.error("license verification failed: {}", exc)
            raise

    def verify_runtime(self, *, now: datetime | None = None) -> None:
        """Re-verify during periodic checks; raise LicenseError on failure."""

        if not self.config.enabled:
            return
        try:
            payload = self._verify(now=now or utc_now())
            self._payload = payload
            self._last_error = None
        except LicenseError as exc:
            self._last_error = exc
            logger.error("license revalidation failed: {}", exc)
            raise

    def start_periodic_revalidation(
        self,
        *,
        on_failure: Callable[[LicenseError], Awaitable[None] | None] | None = None,
    ) -> asyncio.Task[None] | None:
        """启动后台周期复检，防止进程长跑跨过到期点仍继续服务。

        仅启动时 ``verify_startup`` 的话，不重启就只会验一次；本方法按
        ``check_interval_sec`` 反复调用 ``verify_runtime``（验签/绑机/到期/防回拨）。
        失败时先回调 ``on_failure``（接入层可锁请求并硬停进程），再抛出异常结束任务。

        ``enabled=false`` 时返回 ``None`` 且不创建任务；已有未结束任务则复用并返回之。
        """

        if not self.config.enabled:
            return None
        if self._revalidate_task is not None and not self._revalidate_task.done():
            return self._revalidate_task

        async def _loop() -> None:
            interval = self.config.check_interval_sec
            while True:
                await asyncio.sleep(interval)
                try:
                    self.verify_runtime()
                except LicenseError as exc:
                    if on_failure is not None:
                        result = on_failure(exc)
                        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                            await result  # type: ignore[arg-type]
                    raise

        self._revalidate_task = asyncio.create_task(_loop(), name="viknow-license-revalidate")
        return self._revalidate_task

    async def stop_periodic_revalidation(self) -> None:
        task = self._revalidate_task
        self._revalidate_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _verify(self, *, now: datetime) -> LicensePayload:
        """执行一次完整授权校验（启动与周期复检共用），依次检查：

        1. 公钥验签：``license.lic`` 签名合法且载荷可解析；
        2. 绑机：本机 fingerprint 与 license 中的 ``machine_fingerprint`` 一致；
        3. 有效期：``now`` 不早于 ``issued_at``（含 skew）、不晚于 ``expires_at``；
        4. 激活态：更新加密 ``last_seen_at``，发现时钟回拨则失败。

        全部通过返回 ``LicensePayload``；任一失败抛出 ``LicenseError``。
        """

        payload = load_and_verify_license(
            self.config.license_path,
            self.config.public_key_path,
        )
        # 绑机：本机 machine-id 算出的 fingerprint 必须与 license 签发时写入的一致
        fingerprint = fingerprint_from_path(self.config.machine_id_path)
        if fingerprint != payload.machine_fingerprint:
            raise LicenseError(
                LicenseErrorCode.FINGERPRINT_MISMATCH,
                "license machine_fingerprint does not match this host",
            )

        now = now.astimezone(UTC)
        issued = payload.issued_at.astimezone(UTC)
        expires = payload.expires_at.astimezone(UTC)
        if now.timestamp() + self.config.clock_skew_sec < issued.timestamp():
            raise LicenseError(
                LicenseErrorCode.NOT_YET_VALID,
                f"license not yet valid: issued_at={issued.isoformat()}",
            )
        if now.timestamp() > expires.timestamp():
            raise LicenseError(
                LicenseErrorCode.EXPIRED,
                f"license expired at {expires.isoformat()}",
            )

        check_and_update_state(
            self.config.state_dir,
            fingerprint,
            now=now,
            clock_skew_sec=self.config.clock_skew_sec,
        )
        return payload


def _fingerprint_prefix(value: str | None) -> str | None:
    if not value:
        return None
    return value[:12]
