"""License payload models and error codes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LicenseErrorCode(StrEnum):
    """Structured license failure reasons for logs and health endpoints."""

    DISABLED = "disabled"
    MISSING_FILE = "missing_file"
    INVALID_FORMAT = "invalid_format"
    INVALID_SIGNATURE = "invalid_signature"
    MISSING_PUBLIC_KEY = "missing_public_key"
    MACHINE_ID_MISSING = "machine_id_missing"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    CLOCK_ROLLBACK = "clock_rollback"
    STATE_CORRUPT = "state_corrupt"


class LicenseError(RuntimeError):
    """Raised when license verification fails in private deployment mode."""

    def __init__(self, code: LicenseErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


class LicensePayload(BaseModel):
    """Signed license body (without signature field)."""

    model_config = ConfigDict(extra="forbid")

    license_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    customer_name: str | None = None
    issued_at: datetime
    expires_at: datetime
    machine_fingerprint: str = Field(min_length=1)
    features: dict[str, Any] = Field(default_factory=dict)
    product: str = "viknow"
    version_range: str | None = None


class LicenseDocument(BaseModel):
    """On-disk license.lic document: payload fields + signature."""

    model_config = ConfigDict(extra="forbid")

    license_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    customer_name: str | None = None
    issued_at: datetime
    expires_at: datetime
    machine_fingerprint: str = Field(min_length=1)
    features: dict[str, Any] = Field(default_factory=dict)
    product: str = "viknow"
    version_range: str | None = None
    signature: str = Field(min_length=1)

    def payload(self) -> LicensePayload:
        return LicensePayload(
            license_id=self.license_id,
            customer_id=self.customer_id,
            customer_name=self.customer_name,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            machine_fingerprint=self.machine_fingerprint,
            features=self.features,
            product=self.product,
            version_range=self.version_range,
        )


class LicenseStatus(BaseModel):
    """Non-sensitive license summary for health / ops."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    valid: bool
    code: LicenseErrorCode | None = None
    customer_id: str | None = None
    license_id: str | None = None
    expires_at: datetime | None = None
    fingerprint_prefix: str | None = None
