"""离线 License 验签（仅运行时；不含密钥生成与签名）。

签发侧（generate / sign）见 ``tools/license/signing.py``，不得打进客户镜像。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from viknow.deployment.license.models import (
    LicenseDocument,
    LicenseError,
    LicenseErrorCode,
    LicensePayload,
)


def load_public_key(pem: bytes | Path) -> Ed25519PublicKey:
    """从 PEM 加载 Ed25519 公钥（镜像内嵌公钥）。"""

    data = pem.read_bytes() if isinstance(pem, Path) else pem
    key = load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("license public key must be Ed25519")
    return key


def canonical_payload_bytes(payload: LicensePayload | dict[str, Any]) -> bytes:
    """载荷的规范 JSON 字节（签/验必须使用同一算法）。

    签发侧 ``tools/license/signing.py`` 复用此函数，避免实现分叉。
    """

    data = payload.model_dump(mode="json") if isinstance(payload, LicensePayload) else payload
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def verify_document(document: LicenseDocument, public_key: Ed25519PublicKey) -> LicensePayload:
    """用公钥校验签名，成功则返回 payload。"""

    payload = document.payload()
    try:
        signature = base64.b64decode(document.signature, validate=True)
    except Exception as exc:
        raise LicenseError(
            LicenseErrorCode.INVALID_SIGNATURE,
            "license signature is not valid base64",
        ) from exc
    try:
        public_key.verify(signature, canonical_payload_bytes(payload))
    except InvalidSignature as exc:
        raise LicenseError(
            LicenseErrorCode.INVALID_SIGNATURE,
            "license signature verification failed",
        ) from exc
    return payload


def load_and_verify_license(path: Path, public_key_path: Path) -> LicensePayload:
    """读取 license.lic + 公钥并验签。"""

    if not path.is_file():
        raise LicenseError(
            LicenseErrorCode.MISSING_FILE,
            f"license file not found: {path}",
        )
    if not public_key_path.is_file():
        raise LicenseError(
            LicenseErrorCode.MISSING_PUBLIC_KEY,
            f"public key not found: {public_key_path}",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        document = LicenseDocument.model_validate(raw)
    except Exception as exc:
        raise LicenseError(
            LicenseErrorCode.INVALID_FORMAT,
            f"license file is invalid: {exc}",
        ) from exc
    public_key = load_public_key(public_key_path)
    return verify_document(document, public_key)
