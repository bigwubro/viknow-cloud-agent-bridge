from __future__ import annotations

import pytest
from hyperrag.kg.neo4j_driver import create_async_neo4j_driver, run_with_neo4j_retry
from neo4j.exceptions import ServiceUnavailable


def test_create_async_neo4j_driver_uses_pool_tuning(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeDriver:
        pass

    def fake_driver(uri: str, **config):
        captured["uri"] = uri
        captured.update(config)
        return FakeDriver()

    monkeypatch.setenv("NEO4J_MAX_CONNECTION_LIFETIME", "120")
    monkeypatch.setenv("NEO4J_LIVENESS_CHECK_TIMEOUT", "3.5")
    monkeypatch.setattr(
        "hyperrag.kg.neo4j_driver.AsyncGraphDatabase.driver",
        fake_driver,
    )

    driver = create_async_neo4j_driver("neo4j://example:7687", ("neo4j", "secret"))

    assert isinstance(driver, FakeDriver)
    assert captured["uri"] == "neo4j://example:7687"
    assert captured["auth"] == ("neo4j", "secret")
    assert captured["max_connection_lifetime"] == 120.0
    assert captured["liveness_check_timeout"] == 3.5


@pytest.mark.asyncio
async def test_run_with_neo4j_retry_recovers_after_service_unavailable() -> None:
    attempts = {"count": 0}

    async def reset_driver() -> None:
        attempts["reset"] = True

    async def operation() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ServiceUnavailable("stale connection")
        return "ok"

    result = await run_with_neo4j_retry(
        operation,
        reset_driver=reset_driver,
        operation_name="delete_document_graph",
    )

    assert result == "ok"
    assert attempts["count"] == 2
    assert attempts["reset"] is True


@pytest.mark.asyncio
async def test_run_with_neo4j_retry_reraises_after_max_attempts() -> None:
    async def reset_driver() -> None:
        return None

    async def operation() -> None:
        raise ServiceUnavailable("still broken")

    with pytest.raises(ServiceUnavailable, match="still broken"):
        await run_with_neo4j_retry(
            operation,
            reset_driver=reset_driver,
            operation_name="delete_document_graph",
            max_attempts=2,
        )
