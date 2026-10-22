from __future__ import annotations

from types import SimpleNamespace

import pytest
from hyperrag.kg.constants import (
    PRECOMPILED_CHUNK_ID,
    PRECOMPILED_FILE_PATH,
    PRECOMPILED_LIBRARY_ID,
)
from hyperrag.single_layer_kg import SingleLayerKGService


class _FakeRepo:
    def __init__(self) -> None:
        self.upserted_graph = None

    async def export_graph_records(self, *, workspaces=None):
        return {
            "workspaces": workspaces or [],
            "entities": [
                {
                    "workspace": "default",
                    "entity_name": "青象门机",
                    "entity_type": "Component",
                    "entity_description": "青象内部叫门机",
                    "aliases": ["门机"],
                    "file_path": "/tmp/demo.md",
                    "chunk_ids": ["c1"],
                    "library_id": "default",
                }
            ],
            "relations": [
                {
                    "workspace": "default",
                    "source_entity": "青象门机",
                    "target_entity": "制动单元",
                    "relationship_description": "门机连接制动单元",
                    "situation": "",
                    "relation_keywords": ["门机", "制动单元"],
                    "synonym": False,
                    "file_path": "/tmp/demo.md",
                    "chunk_ids": ["c1"],
                    "library_id": "default",
                }
            ],
        }

    async def upsert_document_graph(self, graph):
        self.upserted_graph = graph
        return {
            "entity_nodes": len(graph.entity_nodes),
            "relation_edges": len(graph.relation_edges),
        }


@pytest.mark.asyncio
async def test_export_precompiled_package_strips_and_desensitizes() -> None:
    service = object.__new__(SingleLayerKGService)
    service.repository = _FakeRepo()
    service.workspace = "PRECOMPILED"
    service.config = SimpleNamespace(kg_vector_enabled=False)

    package = await service.export_precompiled_package(
        workspaces=["default"],
        company_replacements={"青象": "COMPANY_A"},
        alias_fullname_map={"门机": "轿门机"},
    )

    assert package["entities"][0]["name"] == "COMPANY_A轿门机"
    assert package["relations"][0]["description"] == "轿门机连接制动单元"
    assert "file_path" not in package["entities"][0]
    assert package["metadata"]["stripped_fields"]


@pytest.mark.asyncio
async def test_import_precompiled_package_marks_pending_lint() -> None:
    service = object.__new__(SingleLayerKGService)
    repo = _FakeRepo()
    service.repository = repo
    service.workspace = "PRECOMPILED"
    service.config = SimpleNamespace(kg_vector_enabled=False)

    result = await service.import_precompiled_package(
        package={
            "entities": [
                {
                    "name": "电机",
                    "type": "Component",
                    "description": "动力组件",
                    "aliases": ["马达"],
                }
            ],
            "relations": [
                {
                    "source_entity": "电机",
                    "target_entity": "制动单元",
                    "description": "电机驱动制动单元",
                }
            ],
        }
    )

    assert result["imported_entities"] == 2
    assert result["imported_relations"] == 1
    assert repo.upserted_graph is not None
    entity = repo.upserted_graph.entity_nodes[0]
    relation = repo.upserted_graph.relation_edges[0]
    assert entity.file_path == PRECOMPILED_FILE_PATH
    assert entity.library_id == PRECOMPILED_LIBRARY_ID
    assert entity.chunk_ids == [PRECOMPILED_CHUNK_ID]
    assert entity.status == "PENDING_LINT"
    assert relation.status == "PENDING_LINT"
