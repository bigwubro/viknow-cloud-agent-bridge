"""Bounded replay of authoritative, retryable gateway failures before output."""

from __future__ import annotations

import asyncio
import random
import time

import httpx

from agents.expert_buddy.core.model_gateway_protocol import (
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    parse_result,
    publish_message,
)

# The shared provider reported no admission capacity; unlike a transient provider
# error this is a queue condition that needs a longer, bounded backoff.
CAPACITY_UNAVAILABLE_CODE = "GATEWAY_CAPACITY_UNAVAILABLE"


def remaining_run_seconds():
    # tools package imports configuration; resolve only after model construction.
    from agents.expert_buddy.core.tools.context import remaining_run_seconds as remaining

    return remaining()


class GatewayRecovery:
    def __init__(self, route):
        self.route = route
        self.spent = 0
        self.retries = 0
        self.capacity_retries = 0

    @property
    def enabled(self) -> bool:
        """Recovery stays off unless a policy explicitly asks for it."""
        return bool(self.route.max_recovery_attempts or self.route.capacity_recovery_enabled)

    def _capacity_policy(self, code: str) -> tuple[int, float] | None:
        """Capacity-only escalation; ``None`` keeps the plain retry policy."""
        if not self.route.capacity_recovery_enabled or code != CAPACITY_UNAVAILABLE_CODE:
            return None
        return (
            max(self.route.max_recovery_attempts, self.route.capacity_recovery_max_attempts),
            self.route.capacity_recovery_backoff_seconds,
        )

    def prepare(self, request):
        if not self.enabled:
            return
        remaining = remaining_run_seconds()
        if remaining is not None:
            old = request.extensions.get("timeout", {})
            request.extensions["timeout"] = {
                key: min(old[key], remaining) if old.get(key) is not None else remaining
                for key in ("connect", "read", "write", "pool")
            }

    def delay(self, response):
        if not self.enabled or not response.is_error:
            return None
        if response.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
            return None
        try:
            report = parse_result(response.json())
        except (ValueError, RuntimeError):
            return None
        self.spent += max(1, len(report.provider_attempts))
        safe = (
            report.retryable
            and report.submission_state in {"not_submitted", "confirmed_failed"}
            and report.status == "failed"
            and report.trace_complete
            and report.failure_origin in {"gateway", "provider"}
            and not any(
                a.http_status in {400, 401, 403, 404, 422} for a in report.provider_attempts
            )
            and (report.submission_state != "confirmed_failed" or bool(report.provider_attempts))
            and response.status_code not in {400, 401, 403, 404, 422}
            and not any(a.status == "succeeded" for a in report.provider_attempts)
            and not (report.submission_state == "not_submitted" and report.provider_attempts)
        )
        capacity = self._capacity_policy(report.code)
        if capacity is None:
            max_attempts = self.route.max_recovery_attempts
            backoff = self.route.recovery_backoff_seconds
        else:
            max_attempts, backoff = capacity
        if not safe or self.spent >= 1 + max_attempts:
            return None
        remaining = remaining_run_seconds()
        # The run deadline still bounds every replay, capacity escalation included.
        if remaining is None:
            return None
        retries = self.capacity_retries if capacity is not None else self.retries
        delay = backoff * (2**retries)
        delay += random.uniform(0, delay / 4) if delay else 0
        delay = max(delay, report.retry_after_seconds or 0)
        try:
            delay = max(delay, float(response.headers.get("retry-after", "0")))
        except ValueError:
            # An unsupported Retry-After date must not be shortened into an early retry.
            return None
        if delay >= remaining:
            return None
        if capacity is not None:
            self.capacity_retries += 1
        else:
            self.retries += 1
        publish_message(
            {
                "kind": "gateway_recovery",
                "request_id": report.request_id,
                "code": report.code,
                "submission_state": report.submission_state,
                "retry_number": retries + 1,
                "attempts_consumed": self.spent,
                "delay_seconds": delay,
            }
        )
        return delay

    def wait(self, delay):
        end = time.monotonic() + delay
        while time.monotonic() < end:
            remaining_run_seconds()
            time.sleep(max(0, min(0.05, end - time.monotonic())))
        remaining_run_seconds()

    async def await_delay(self, delay):
        end = time.monotonic() + delay
        while time.monotonic() < end:
            remaining_run_seconds()
            await asyncio.sleep(max(0, min(0.05, end - time.monotonic())))
        remaining_run_seconds()


def terminal_model_failure(exc):
    """Transport failures must not become fresh structured-output retry attempts."""
    from openai import APIError

    from agents.expert_buddy.core.model_gateway_protocol import GatewayMessageFailure

    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (APIError, httpx.TransportError, GatewayMessageFailure)):
            return True
        current = current.__cause__ or current.__context__
    return False
