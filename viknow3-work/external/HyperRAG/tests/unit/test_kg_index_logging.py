from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import hyperrag.single_layer_kg as single_layer_kg_module
import hyperrag.task.handler as handler_module
import pytest
from hyperrag.kg.constants import PRECOMPILED_WORKSPACE
from hyperrag.single_layer_kg import (
    ChunkRecord,
    ExtractedEntity,
    ExtractedRelation,
    SingleLayerKGService,
    _build_video_segment_chunk_ids,
)
from hyperrag.task.handler import KGIndexHandler, _attach_video_chunk_ids
from hyperrag.task.models import Task, TaskResource, TaskStatus


def _render_log_message(template: object, args: tuple[object, ...]) -> str:
    if not isinstance(template, str):
        return str(template)
    if not args:
        return template
    return template.format(*args)


@pytest.mark.asyncio
async def test_extract_entities_and_relations_logs_llm_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(SingleLayerKGService)
    service.config = SimpleNamespace(llm_model="demo-model", ER_extraction_max_async=7)

    class FakeExtractor:
        model = "demo-model"

        async def extract_chunk(self, *, chunk, modality: str, title: str):  # type: ignore[no-untyped-def]
            return (
                [
                    ExtractedEntity(
                        extraction_id="ext-1",
                        file_path=chunk.file_path,
                        chunk_id=chunk.chunk_id,
                        entity_name="门锁",
                        entity_type="设备",
                        entity_description="门锁组件",
                    )
                ],
                [
                    ExtractedRelation(
                        extraction_id="rel-1",
                        file_path=chunk.file_path,
                        chunk_id=chunk.chunk_id,
                        source_entity="门锁",
                        target_entity="门机",
                        relationship_description="门锁连接门机",
                        situation="检修阶段",
                        relation_keywords=["门锁", "门机"],
                    )
                ],
            )

    service._get_extractor = (  # type: ignore[method-assign]
        lambda *args, **kwargs: FakeExtractor()
    )

    messages: list[str] = []

    def fake_info(template: object, *args: object) -> None:
        messages.append(_render_log_message(template, args))

    monkeypatch.setattr(single_layer_kg_module.logger, "info", fake_info)

    entities, relations, failed_chunks = await service._extract_entities_and_relations(
        asset_id="asset-1",
        chunks=[
            ChunkRecord(
                chunk_id="chunk-1",
                file_path="/tmp/demo.md",
                order=0,
                raw_content="原文",
                text="检修门锁与门机",
                tokens=10,
                segment_ids=["seg-1"],
            )
        ],
        modality="document",
        title="演示文档",
    )

    assert len(entities) == 1
    assert len(relations) == 1
    assert failed_chunks == []
    assert any(
        "stage=entity_relation_extraction started asset=asset-1 chunks=1 llm_model=demo-model ER_extraction_max_async=7"
        in message
        for message in messages
    )
    assert any(
        "stage=entity_relation_extraction completed asset=asset-1 chunks=1 entities=1 relations=1"
        in message
        for message in messages
    )


def _build_kg_index_task(tmp_path: Path) -> Task:
    return Task(
        task_id="task-1",
        task_type="kg_index",
        workspace="ws",
        asset_id="asset-1",
        status=TaskStatus.PENDING,
        resource=TaskResource(),
        metadata={
            "asset_path": str(tmp_path / "demo.md"),
            "asset": {"title": "演示文档"},
            "segments": [{"segment_id": "seg-1"}],
            "fast_ingest": False,
        },
    )


def test_video_segment_chunk_id_helpers_preserve_first_mapping() -> None:
    chunks = [
        ChunkRecord(
            chunk_id="chunk-1",
            file_path="/tmp/demo.mp4",
            order=0,
            raw_content="first",
            text="first",
            tokens=1,
            segment_ids=["seg-1"],
        ),
        ChunkRecord(
            chunk_id="chunk-2",
            file_path="/tmp/demo.mp4",
            order=1,
            raw_content="second",
            text="second",
            tokens=1,
            segment_ids=["seg-2", "seg-1"],
        ),
    ]

    assert _build_video_segment_chunk_ids("video", chunks) == {
        "seg-1": "chunk-1",
        "seg-2": "chunk-2",
    }
    assert _build_video_segment_chunk_ids("document", chunks) == {}
    assert _attach_video_chunk_ids(
        [{"segment_id": "seg-1", "extra": {"source_id": "asset-1"}}],
        {"seg-1": "chunk-1"},
    ) == [
        {
            "segment_id": "seg-1",
            "extra": {"source_id": "asset-1", "chunk_id": "chunk-1"},
        }
    ]


@pytest.mark.asyncio
async def test_kg_index_handler_logs_start_and_stage_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_payload = {
        "document_summary": "summary",
        "timings": {
            "parsing_chunking_sec": 1.2,
            "entity_relation_extraction_sec": 12.3,
            "entity_resolution_sec": 3.4,
            "neo4j_upsert_sec": 4.5,
            "vectorization_sec": 5.6,
            "vector_upsert_sec": 6.7,
            "total_sec": 33.3,
        },
        "counters": {
            "chunks": 38,
            "extracted_entities": 120,
            "extracted_relations": 82,
            "entity_nodes": 76,
            "relation_edges": 54,
        },
        "segment_chunk_ids": {"seg-1": "chunk-1"},
    }

    class FakeExperiment:
        def __init__(self) -> None:
            self.config = SimpleNamespace(llm_model="demo-model", ER_extraction_max_async=9)

        async def index_preparsed_document(self, **_kwargs):
            return result_payload

    class FakeKnowledgebaseInfo:
        summarization = "kb summary"

    class FakeHyperRAGV2:
        config = SimpleNamespace(
            fast_ingest_enabled=False,
            fast_ingest_compile_phase="FAST_INGEST",
        )

        async def _get_kg_experiment(self, workspace: str):
            assert workspace == "ws"
            return FakeExperiment()

        async def async_knowledgebase_summary(self, *args, **kwargs):
            return FakeKnowledgebaseInfo()

    messages: list[str] = []

    def fake_info(template: object, *args: object) -> None:
        messages.append(_render_log_message(template, args))

    monkeypatch.setattr(handler_module.logger, "info", fake_info)

    handler = KGIndexHandler(FakeHyperRAGV2())
    result = await handler(_build_kg_index_task(tmp_path))

    assert result.status == "completed"
    assert any(
        "KG index task started: task=task-1 asset=asset-1 workspace=ws mode=preparsed index_route=standard segments=1 llm_model=demo-model fast_ingest_model_active=False ER_extraction_max_async=9"
        in message
        for message in messages
    )
    assert any(
        "KG index stage summary: task=task-1 asset=asset-1 chunks=38 extracted_entities=120 extracted_relations=82 entity_nodes=76 relation_edges=54 parsing_chunking_sec=1.2 entity_relation_extraction_sec=12.3 entity_resolution_sec=3.4 neo4j_upsert_sec=4.5 vectorization_sec=5.6 vector_upsert_sec=6.7 total_sec=33.3"
        in message
        for message in messages
    )
    assert result.data["segments"][0]["extra"]["chunk_id"] == "chunk-1"


@pytest.mark.asyncio
async def test_kg_index_handler_uses_fast_ingest_model_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeExperiment:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                llm_model="Qwen/Qwen3-4B-Instruct-FP8", ER_extraction_max_async=9
            )

        async def index_preparsed_document(self, **_kwargs):
            return {"document_summary": "", "timings": {}, "counters": {}, "artifacts": {}}

    class FakeKnowledgebaseInfo:
        summarization = ""

    class FakeHyperRAGV2:
        config = SimpleNamespace(
            fast_ingest_enabled=True,
            fast_ingest_decoding_strategy="single_model",
            fast_ingest_compile_phase="FAST_INGEST",
            fast_ingest_model="Qwen/Qwen3-4B-Instruct-FP8",
            fast_ingest_base_url="http://127.0.0.1:8505/v1",
            fast_ingest_api_key="fast-key",
        )

        async def _get_kg_experiment(
            self,
            workspace: str,
            *,
            llm_model: str | None = None,
            llm_base_url: str | None = None,
            llm_api_key: str | None = None,
        ):
            captured["workspace"] = workspace
            captured["llm_model"] = llm_model
            captured["llm_base_url"] = llm_base_url
            captured["llm_api_key"] = llm_api_key
            return FakeExperiment()

        async def async_knowledgebase_summary(self, *args, **kwargs):
            return FakeKnowledgebaseInfo()

    monkeypatch.setattr(handler_module.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(handler_module.logger, "warning", lambda *args, **kwargs: None)

    handler = KGIndexHandler(FakeHyperRAGV2())
    task = _build_kg_index_task(tmp_path)
    task.metadata["fast_ingest"] = True
    result = await handler(task)

    assert result.status == "completed"
    assert captured["workspace"] == "ws"
    assert captured["llm_model"] == "Qwen/Qwen3-4B-Instruct-FP8"
    assert captured["llm_base_url"] == "http://127.0.0.1:8505/v1"
    assert captured["llm_api_key"] == "fast-key"


@pytest.mark.asyncio
async def test_kg_index_handler_uses_fast_ingest_route_with_chunk_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeExperiment:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                llm_model="Qwen/Qwen3-4B-Instruct-FP8", ER_extraction_max_async=9
            )

        async def index_preparsed_document(self, **kwargs):
            captured["persist_to_kg"] = kwargs.get("persist_to_kg")
            captured["persist_chunk_vectors"] = kwargs.get("persist_chunk_vectors")
            captured["include_relation_edges"] = kwargs.get("include_relation_edges")
            return {
                "document_summary": "",
                "timings": {},
                "counters": {},
                "relation_edges": [
                    {
                        "source_entity": "A",
                        "target_entity": "B",
                        "relationship_description": "A关联B",
                        "relation_type": "关联",
                        "chunk_ids": ["chunk-1"],
                    }
                ],
            }

    class FakeKnowledgebaseInfo:
        summarization = ""

    class FakeHyperRAGV2:
        config = SimpleNamespace(
            fast_ingest_enabled=True,
            fast_ingest_decoding_strategy="single_model",
            fast_ingest_compile_phase="FAST_INGEST",
            fast_ingest_model="Qwen/Qwen3-4B-Instruct-FP8",
            fast_ingest_base_url="http://127.0.0.1:8505/v1",
            fast_ingest_api_key="fast-key",
        )

        async def _get_kg_experiment(self, workspace: str, **_kwargs):
            return FakeExperiment()

        async def async_knowledgebase_summary(self, *args, **kwargs):
            return FakeKnowledgebaseInfo()

        async def aingest_fast_relations(self, **kwargs):
            captured["ingest_kwargs"] = kwargs
            return {"created_edges": 1, "absorbed_edges": 0, "duplicate_absorb_rate": 0.0}

    monkeypatch.setattr(handler_module.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(handler_module.logger, "warning", lambda *args, **kwargs: None)

    handler = KGIndexHandler(FakeHyperRAGV2())
    task = _build_kg_index_task(tmp_path)
    task.metadata["fast_ingest"] = True
    result = await handler(task)

    assert result.status == "completed"
    assert captured["persist_to_kg"] is False
    assert captured["persist_chunk_vectors"] is True
    assert captured["include_relation_edges"] is True
    assert captured["ingest_kwargs"]["doc_id"] == "asset-1"


@pytest.mark.asyncio
async def test_kg_index_handler_uses_precompile_workspace_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeExperiment:
        def __init__(self) -> None:
            self.config = SimpleNamespace(llm_model="demo-model", ER_extraction_max_async=9)

        async def index_preparsed_document(self, **kwargs):
            captured["persist_to_kg"] = kwargs.get("persist_to_kg")
            captured["include_relation_edges"] = kwargs.get("include_relation_edges")
            captured["pre_compile_mode"] = kwargs.get("pre_compile_mode")
            captured["retry_er_chunk_ids"] = kwargs.get("retry_er_chunk_ids")
            return {
                "document_summary": "",
                "timings": {},
                "counters": {},
                "failed_er_chunks": [
                    {"chunk_id": "chunk-failed", "error": "missing entity_description"}
                ],
            }

    class FakeKnowledgebaseInfo:
        summarization = ""

    class FakeHyperRAGV2:
        config = SimpleNamespace(
            fast_ingest_enabled=True,
            fast_ingest_decoding_strategy="single_model",
            fast_ingest_compile_phase="FAST_INGEST",
            fast_ingest_model="Qwen/Qwen3-4B-Instruct-FP8",
            fast_ingest_base_url="http://127.0.0.1:8505/v1",
            fast_ingest_api_key="fast-key",
        )

        async def _get_kg_experiment(self, workspace: str, **_kwargs):
            captured["workspace"] = workspace
            return FakeExperiment()

        async def async_knowledgebase_summary(self, *args, **kwargs):
            return FakeKnowledgebaseInfo()

    monkeypatch.setattr(handler_module.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(handler_module.logger, "warning", lambda *args, **kwargs: None)
    status_updates: list[dict[str, object]] = []

    class FakeStatusStorage:
        async def set_status(self, *args, **kwargs):
            status_updates.append(kwargs)

    monkeypatch.setattr(handler_module, "get_asset_status_storage", lambda: FakeStatusStorage())

    handler = KGIndexHandler(FakeHyperRAGV2())
    task = _build_kg_index_task(tmp_path)
    task.metadata["pre_compile"] = True
    task.metadata["retry_er_chunk_ids"] = ["chunk-failed"]
    result = await handler(task)

    assert result.status == "completed"
    assert captured["workspace"] == PRECOMPILED_WORKSPACE
    assert captured["persist_to_kg"] is True
    assert captured["include_relation_edges"] is False
    assert captured["pre_compile_mode"] is True
    assert captured["retry_er_chunk_ids"] == ["chunk-failed"]
    assert result.error is not None
    assert "chunk-failed" in result.error
    assert result.data["failed_er_chunks"][0]["chunk_id"] == "chunk-failed"
    assert (
        status_updates[0]["metadata"]["precompile_failed_er_chunks"][0]["chunk_id"]
        == "chunk-failed"
    )
