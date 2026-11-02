from __future__ import annotations

import pytest
from hyperrag.kg.repository_cleanup import clear_workspace, clear_workspaces_by_prefix


class _FakeResult:
    def __init__(self, row):
        self._row = row

    async def single(self):
        return self._row


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        row = self.rows[len(self.calls) - 1] if len(self.calls) <= len(self.rows) else None
        return _FakeResult(row)


class _FakeDriver:
    def __init__(self, session):
        self._session = session
        self.last_database = None

    def session(self, *, database=None):
        self.last_database = database
        return self._session


@pytest.mark.asyncio
async def test_clear_workspace_returns_summary_counts() -> None:
    session = _FakeSession([{"entities": 3, "relations": 4, "issues": 1}])
    driver = _FakeDriver(session)

    payload = await clear_workspace(
        driver=driver,
        database="neo4j",
        workspace="default",
        entity_label="TLKGEntity",
        relation_type="TLKG_RELATION",
        lint_issue_label="TLKGLintIssue",
    )

    assert driver.last_database == "neo4j"
    assert payload["workspace"] == "default"
    assert payload["deleted"] is True
    assert payload["entities"] == 3
    assert payload["relations"] == 4
    assert payload["issues"] == 1
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_clear_workspaces_by_prefix_returns_workspaces_and_counts() -> None:
    session = _FakeSession([{"workspaces": ["a", "b"], "entities": 5, "relations": 7, "issues": 2}])
    driver = _FakeDriver(session)

    payload = await clear_workspaces_by_prefix(
        driver=driver,
        database="neo4j",
        workspace_prefix="tenant-",
        entity_label="TLKGEntity",
        relation_type="TLKG_RELATION",
        lint_issue_label="TLKGLintIssue",
    )

    assert payload["workspace_prefix"] == "tenant-"
    assert payload["workspaces"] == ["a", "b"]
    assert payload["deleted"] is True
    assert payload["entities"] == 5
    assert payload["relations"] == 7
    assert payload["issues"] == 2
    assert len(session.calls) == 3
