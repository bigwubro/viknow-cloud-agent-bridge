"""加密激活态：在本地持久化授权时间线，抑制简单的时钟回拨绕过。

每次 ``LicenseGuard`` 校验成功后，将 ``activated_at`` / ``last_seen_at`` 加密写入
state 目录（默认 compose 卷）。下次若系统时间明显早于 ``last_seen_at``（超出
``clock_skew_sec``），判定回拨并失败。

密钥由 machine fingerprint + 内置 salt 派生：换机或篡改密文会导致解密失败
（``STATE_CORRUPT``）。本模块不负责验签、绑机比对或到期判断。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from viknow.deployment.license.models import LicenseError, LicenseErrorCode

_STATE_FILENAME = "activation.json.enc"
_SALT = b"viknow-license-state-v1"


def _fernet_for_fingerprint(fingerprint: str) -> Fernet:
    digest = hashlib.sha256(_SALT + fingerprint.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_FILENAME


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_state(state_dir: Path, fingerprint: str) -> dict[str, datetime] | None:
    """Load activation state or return None if missing."""

    path = _state_path(state_dir)
    if not path.is_file():
        return None
    try:
        token = path.read_bytes()
        plaintext = _fernet_for_fingerprint(fingerprint).decrypt(token)
        data = json.loads(plaintext.decode("utf-8"))
        return {
            "activated_at": datetime.fromisoformat(data["activated_at"]),
            "last_seen_at": datetime.fromisoformat(data["last_seen_at"]),
        }
    except (InvalidToken, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise LicenseError(
            LicenseErrorCode.STATE_CORRUPT,
            f"license state is corrupt or not bound to this machine: {exc}",
        ) from exc


def save_state(
    state_dir: Path,
    fingerprint: str,
    *,
    activated_at: datetime,
    last_seen_at: datetime,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "activated_at": activated_at.astimezone(UTC).isoformat(),
        "last_seen_at": last_seen_at.astimezone(UTC).isoformat(),
    }
    token = _fernet_for_fingerprint(fingerprint).encrypt(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    path = _state_path(state_dir)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(token)
    tmp.replace(path)


def check_and_update_state(
    state_dir: Path,
    fingerprint: str,
    *,
    now: datetime,
    clock_skew_sec: int,
) -> None:
    """Create or update activation state; raise on clock rollback."""

    now = now.astimezone(UTC)
    existing = load_state(state_dir, fingerprint)
    if existing is None:
        save_state(state_dir, fingerprint, activated_at=now, last_seen_at=now)
        return

    last_seen = existing["last_seen_at"].astimezone(UTC)
    earliest_allowed = last_seen.timestamp() - clock_skew_sec
    if now.timestamp() < earliest_allowed:
        raise LicenseError(
            LicenseErrorCode.CLOCK_ROLLBACK,
            f"system clock rolled back: now={now.isoformat()} "
            f"last_seen={last_seen.isoformat()} skew={clock_skew_sec}s",
        )

    save_state(
        state_dir,
        fingerprint,
        activated_at=existing["activated_at"],
        last_seen_at=now,
    )
