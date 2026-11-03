from __future__ import annotations

import pytest
from hyperrag.kg.repository_read import (
    show_document_graph,
    show_graph_summary,
    show_synonyms,
)


class _FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []
        self._iter = iter(self._rows)

    async def single(self):
        return self._row

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:  # pragma: no cover
            raise StopAsyncIteration from exc


class _FakeRecord:
    def __init__(self, payload):
        self._payload = payload

    def data(self):
        return self._payload


class _FakeSession:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.results[len(self.calls) - 1]


class _FakeDriver:
    def __init__(self, session):
        self._session = session
        self.last_database = None

    def session(self, *, database=None):
        self.last_database = database
        return self._session


@pytest.mark.asyncio
async def test_show_document_graph_returns_entities_and_relations() -> None:
    session = _FakeSession(
        [
            _FakeResult(
                row={
                    "entities": [{"entity_name": "电机"}],
                    "relations": [{"source_entity": "电机", "target_entity": "制动"}],
                }
            )
        ]
    )
    driver = _FakeDriver(session)

    payload = await show_document_graph(
        driver=driver,
        database="neo4j",
        workspace="default",
        file_path="a.md",
        entity_label="TLKGEntity",
        relation_type="TLKG_RELATION",
    )

    assert driver.last_database == "neo4j"
    assert payload["file_path"] == "a.md"
    assert payload["entities"][0]["entity_name"] == "电机"
    assert payload["relations"][0]["target_entity"] == "制动"


@pytest.mark.asyncio
async def test_show_document_graph_raises_for_missing_entities() -> None:
    session = _FakeSession([_FakeResult(row={"entities": []})])
    driver = _FakeDriver(session)

    with pytest.raises(ValueError, match="Document graph not found: missing.md"):
        await show_document_graph(
            driver=driver,
            database="neo4j",
            workspace="default",
            file_path="missing.md",
            entity_label="TLKGEntity",
            relation_type="TLKG_RELATION",
        )


@pytest.mark.asyncio
async def test_show_synonyms_returns_rows() -> None:
    session = _FakeSession(
        [
            _FakeResult(
                rows=[
                    _FakeRecord(
                        {
                            "source_entity_name": "A",
                            "target_entity_name": "B",
                            "relationship_description": "同义",
                        }
                    )
                ]
            )
        ]
    )
    driver = _FakeDriver(session)

    payload = await show_synonyms(
        driver=driver,
        database="neo4j",
        workspace="default",
        file_path=None,
        limit=10,
        entity_label="TLKGEntity",
        relation_type="TLKG_RELATION",
    )

    assert payload["workspace"] == "default"
    assert payload["synonyms"][0]["source_entity_name"] == "A"
    _, params = session.calls[0]
    assert params["limit"] == 10


@pytest.mark.asyncio
async def test_show_graph_summary_converts_counts_to_int() -> None:
    session = _FakeSession(
        [
            _FakeResult(
                row={
                    "entity_count": 3,
                    "ordinary_relation_count": 2,
                    "synonym_relation_count": 1,
                    "document_count": 1,
                }
            )
        ]
    )
    driver = _FakeDriver(session)

    payload = await show_graph_summary(
        driver=driver,
        database="neo4j",
        workspace="default",
        entity_label="TLKGEntity",
        relation_type="TLKG_RELATION",
    )

    assert payload == {
        "workspace": "default",
        "entity_count": 3,
        "ordinary_relation_count": 2,
        "synonym_relation_count": 1,
        "document_count": 1,
    }
