"""Time-limited download grants (HMAC). No Bearer required on GET with ?grant=."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import quote

DEFAULT_GRANT_TTL_SECONDS = 86400
MAX_GRANT_TTL_SECONDS = 86400


class GrantError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def grant_secret(settings_secret: str) -> bytes:
    secret = (settings_secret or os.environ.get("STORAGE_GRANT_SECRET") or "").strip()
    if not secret:
        raise GrantError("grant_secret_not_configured")
    return secret.encode("utf-8")


def issue_download_grant(
    *,
    library_id: str,
    asset_id: str,
    ttl_seconds: int,
    secret: bytes,
) -> tuple[str, int]:
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    if not library_id or not asset_id:
        raise GrantError("invalid_asset")
    ttl = int(ttl_seconds)
    if ttl <= 0 or ttl > MAX_GRANT_TTL_SECONDS:
        ttl = DEFAULT_GRANT_TTL_SECONDS
    exp = int(time.time()) + ttl
    payload = {"lib": library_id, "asset": asset_id, "exp": exp}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64url_encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", exp


def verify_download_grant(
    token: str,
    *,
    library_id: str,
    asset_id: str,
    secret: bytes,
) -> None:
    text = str(token or "").strip()
    if not text or "." not in text:
        raise GrantError("invalid_grant")
    body, sig = text.rsplit(".", 1)
    expected = _b64url_encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise GrantError("invalid_grant")
    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(body))
    except (json.JSONDecodeError, ValueError) as exc:
        raise GrantError("invalid_grant") from exc
    if str(payload.get("lib") or "") != str(library_id or "").strip():
        raise GrantError("grant_mismatch")
    if str(payload.get("asset") or "") != str(asset_id or "").strip():
        raise GrantError("grant_mismatch")
    exp = int(payload.get("exp") or 0)
    if exp < int(time.time()):
        raise GrantError("grant_expired")


def build_grant_content_url(
    download_base_url: str,
    library_id: str,
    asset_id: str,
    grant_token: str,
) -> str:
    base = str(download_base_url or "").strip().rstrip("/")
    if not base:
        raise GrantError("download_base_not_configured")
    path = f"/libraries/{quote(library_id, safe='')}/assets/{quote(asset_id, safe='')}/content"
    return f"{base}{path}?grant={quote(grant_token, safe='')}"
