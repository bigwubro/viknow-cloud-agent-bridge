"""Sanitized Feishu connector errors."""

from __future__ import annotations


class FeishuIntegrationError(RuntimeError):
    """Base error safe to classify at the ViKnow boundary."""


class FeishuConfigurationError(FeishuIntegrationError):
    """Raised when the read-only connector is disabled or incomplete."""


class FeishuApiError(FeishuIntegrationError):
    """Feishu response failure without request secrets or raw response bodies."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable

    @property
    def permission_denied(self) -> bool:
        return self.http_status == 403 or self.code in {99991672, 1069902}

    @property
    def not_found(self) -> bool:
        return self.http_status in {404, 410} or self.code in {1069906, 1069914}
