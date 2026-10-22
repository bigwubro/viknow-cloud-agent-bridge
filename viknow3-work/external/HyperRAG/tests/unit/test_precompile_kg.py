from __future__ import annotations

from hyperrag.kg.constants import (
    PRECOMPILED_CHUNK_ID,
    PRECOMPILED_FILE_PATH,
    PRECOMPILED_LIBRARY_ID,
)
from hyperrag.single_layer_kg import DocumentGraph, GraphEntityNode, GraphRelationEdge, SingleLayerKGService


def test_mark_graph_as_precompiled_sets_expected_markers() -> None:
    graph = DocumentGraph(
        workspace="demo",
        file_path="/tmp/demo.md",
        source_uri="/tmp/demo.md",
        modality="document",
        title="demo",
        asset={},
        segments=[],
        chunks=[],
        extracted_entities=[],
        extracted_relations=[],
        entity_nodes=[
            GraphEntityNode(
                node_id="n-1",
                entity_name="电梯",
                entity_type="Concept",
                entity_description="电梯系统",
                file_path="/tmp/demo.md",
            )
        ],
        relation_edges=[
            GraphRelationEdge(
                relation_id="r-1",
                source_entity="电梯",
                target_entity="门机",
                relationship_description="电梯关联门机",
                situation="",
                relation_keywords=[],
                synonym=False,
                file_path="/tmp/demo.md",
            )
        ],
    )

    SingleLayerKGService._mark_graph_as_precompiled(graph)

    assert graph.file_path == PRECOMPILED_FILE_PATH
    assert graph.entity_nodes[0].status == "PENDING_LINT"
    assert graph.entity_nodes[0].library_id == PRECOMPILED_LIBRARY_ID
    assert graph.entity_nodes[0].chunk_ids == [PRECOMPILED_CHUNK_ID]
    assert graph.relation_edges[0].status == "PENDING_LINT"
    assert graph.relation_edges[0].library_id == PRECOMPILED_LIBRARY_ID
    assert graph.relation_edges[0].chunk_ids == [PRECOMPILED_CHUNK_ID]
