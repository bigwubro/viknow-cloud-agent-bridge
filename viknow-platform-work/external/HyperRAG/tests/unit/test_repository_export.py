from __future__ import annotations

import pytest
from hyperrag.kg.repository_export import export_graph_records


class _FakeResult:
    def __init__(self, row):
        self._row = row

    async def single(self):
        return self._row


class _FakeSession:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        row = self.records[len(self.calls) - 1]
        return _FakeResult(row)


class _FakeDriver:
    def __init__(self, session):
        self._session = session
        self.last_database = None

    def session(self, *, database=None):
        self.last_database = database
        return self._session


@pytest.mark.asyncio
async def test_export_graph_records_filters_workspaces_and_returns_rows() -> None:
    fake_session = _FakeSession(
        [
            {"entities": [{"entity_name": "电机"}]},
            {"relations": [{"source_entity": "电机", "target_entity": "制动单元"}]},
        ]
    )
    fake_driver = _FakeDriver(fake_session)

    payload = await export_graph_records(
        driver=fake_driver,
        database="neo4j",
        entity_label="TLKGEntity",
        relation_type="TLKG_RELATION",
        workspaces=[" default ", "", "PRECOMPILED"],
    )

    assert fake_driver.last_database == "neo4j"
    assert payload["workspaces"] == ["default", "PRECOMPILED"]
    assert payload["entities"][0]["entity_name"] == "电机"
    assert payload["relations"][0]["source_entity"] == "电机"
    assert len(fake_session.calls) == 2
    _, params = fake_session.calls[0]
    assert params["include_all"] is False
    assert params["workspaces"] == ["default", "PRECOMPILED"]
