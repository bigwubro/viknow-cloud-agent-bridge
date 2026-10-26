"""Monitoring API client errors."""


class MonitoringApiError(RuntimeError):
    """A sanitized error safe to expose outside the HTTP client boundary."""
