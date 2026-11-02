from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hyperrag.asset_status import AssetProcessingStatus, AssetStatus
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.knowledgebase_info import KnowledgebaseInfo
from hyperrag.knowledgebase_summary import EMPTY_KNOWLEDGEBASE_SUMMARY
from hyperrag.task.models import TaskStatus, TaskType


def _build_status(
    *,
    asset_id: str,
    status: AssetStatus = AssetStatus.COMPLETED,
    parse_mode: str = "document",
    file_path: str = "/tmp/demo.pdf",
    updated_at: float = 10.0,
    file_data: dict | None = None,
) -> AssetProcessingStatus:
    return AssetProcessingStatus(
        asset_id=asset_id,
        status=status,
        created_at=1.0,
        updated_at=updated_at,
        parse_mode=parse_mode,
        file_path=file_path,
        file_data=file_data,
    )


def _build_hyperrag() -> HyperRAGV2:
    hyperrag = object.__new__(HyperRAGV2)
    hyperrag.config = SimpleNamespace(summary_max_tokens=500)
    hyperrag._user_on_task_callback = None
    return hyperrag


@pytest.mark.asyncio
async def test_aget_asset_metadata_prefers_persisted_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(asset_id="doc-1")
    status.metadata = {"asset_type": "doc", "page_count": 12}
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status), update_metadata=AsyncMock()
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    result = await hyperrag.aget_asset_metadata("kb-demo", "doc-1")

    assert result == {
        "asset_type": "doc",
        "page_count": 12,
    }
    storage.update_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_aget_asset_metadata_persists_fallback_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(asset_id="doc-1")
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status), update_metadata=AsyncMock()
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    hyperrag._get_workspace_cache_dirs = lambda _workspace: (  # type: ignore[method-assign]
        "/tmp/mineru",
        "/tmp/video",
    )
    hyperrag._load_mineru_cache_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=({"page_info": {"total_pages": 12}}, [])
    )

    result = await hyperrag.aget_asset_metadata("kb-demo", "doc-1")

    assert result == {
        "asset_type": "doc",
        "page_count": 12,
    }
    storage.update_metadata.assert_awaited_once_with(
        "kb-demo",
        "doc-1",
        {"asset_type": "doc", "page_count": 12},
    )


@pytest.mark.asyncio
async def test_aget_asset_metadata_falls_back_when_status_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SimpleNamespace(
        get_status=AsyncMock(side_effect=RuntimeError("database unavailable")),
        update_metadata=AsyncMock(),
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    hyperrag._get_workspace_cache_dirs = lambda _workspace: (  # type: ignore[method-assign]
        "/tmp/mineru",
        "/tmp/video",
    )
    hyperrag._load_video_cache_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, [], [])
    )
    hyperrag._load_mineru_cache_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=({"page_info": {"total_pages": 8}}, [])
    )

    result = await hyperrag.aget_asset_metadata("kb-demo", "doc-1")

    assert result == {
        "asset_type": "doc",
        "page_count": 8,
    }
    storage.update_metadata.assert_awaited_once_with(
        "kb-demo",
        "doc-1",
        {"asset_type": "doc", "page_count": 8},
    )


@pytest.mark.asyncio
async def test_aget_asset_metadata_persists_video_duration_from_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(asset_id="video-1", parse_mode="video")
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status), update_metadata=AsyncMock()
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    hyperrag.aget_video_semantic = AsyncMock(return_value=None)  # type: ignore[method-assign]
    hyperrag._get_workspace_cache_dirs = lambda _workspace: (  # type: ignore[method-assign]
        "/tmp/mineru",
        "/tmp/video",
    )
    hyperrag._load_video_cache_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=({"extra": {"duration_sec": 95.5}}, [], [])
    )

    result = await hyperrag.aget_asset_metadata("kb-demo", "video-1")

    assert result == {
        "asset_type": "video",
        "video_duration": 95.5,
    }
    storage.update_metadata.assert_awaited_once_with(
        "kb-demo",
        "video-1",
        {
            "asset_type": "video",
            "video_duration": 95.5,
        },
    )


@pytest.mark.asyncio
async def test_aget_asset_status_persists_missing_chunk_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(asset_id="doc-1")
    status.metadata = {"doc_type": "document", "asset_type": "doc", "page_count": 12}
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status),
        update_metadata=AsyncMock(),
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    hyperrag.aget_chunk_count = AsyncMock(return_value=37)  # type: ignore[method-assign]

    result = await hyperrag.aget_asset_status("kb-demo", "doc-1")

    assert result is status
    assert result.metadata == {
        "asset_type": "doc",
        "page_count": 12,
        "chunk_nums": 37,
    }
    storage.update_metadata.assert_awaited_once_with(
        "kb-demo",
        "doc-1",
        {"chunk_nums": 37},
        remove_keys=["doc_type"],
    )


@pytest.mark.asyncio
async def test_parse_task_completion_persists_asset_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(asset_id="doc-1", status=AssetStatus.PARSING)
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status), update_metadata=AsyncMock()
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    hyperrag._get_workspace_cache_dirs = lambda _workspace: (  # type: ignore[method-assign]
        "/tmp/mineru",
        "/tmp/video",
    )
    hyperrag._load_mineru_cache_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=({"page_info": {"total_pages": 12}}, [])
    )
    task = SimpleNamespace(
        workspace="kb-demo",
        asset_id="doc-1",
        task_type=TaskType.DOC_PARSE,
        job_name="document",
        error_message=None,
        metadata={},
        result={
            "asset": {
                "doc_id": "doc-1",
                "modality": "document",
                "page_info": {"total_pages": 12},
            },
        },
    )

    await hyperrag._on_task_callback(task, TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value)

    storage.update_metadata.assert_awaited_once_with(
        "kb-demo",
        "doc-1",
        {"asset_type": "doc", "page_count": 12},
    )


@pytest.mark.asyncio
async def test_video_parse_task_completion_persists_video_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(
        asset_id="video-1",
        status=AssetStatus.PARSING,
        parse_mode="video",
    )
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status),
        update_metadata=AsyncMock(),
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    task = SimpleNamespace(
        workspace="kb-demo",
        asset_id="video-1",
        task_type=TaskType.VIDEO_PARSE,
        job_name="video",
        error_message=None,
        metadata={},
        result={
            "asset": {
                "doc_id": "video-1",
                "modality": "video",
                "extra": {},
            },
            "sop_full": {
                "metadata": {
                    "video_info": {
                        "duration_sec": 95.5,
                    }
                }
            },
        },
    )

    await hyperrag._on_task_callback(task, TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value)

    storage.update_metadata.assert_any_await(
        "kb-demo",
        "video-1",
        {"asset_type": "video", "video_duration": 95.5},
    )


@pytest.mark.asyncio
async def test_kg_task_completion_persists_chunk_count_and_asset_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _build_status(asset_id="doc-1", status=AssetStatus.INDEXING)
    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status), update_metadata=AsyncMock()
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)

    hyperrag = _build_hyperrag()
    task = SimpleNamespace(
        workspace="kb-demo",
        asset_id="doc-1",
        task_type=TaskType.KG_INDEX,
        job_name="document",
        error_message=None,
        metadata={},
        result={
            "asset": {
                "doc_id": "doc-1",
                "modality": "document",
                "page_info": {"total_pages": 12},
            },
            "counters": {"chunks": 37},
        },
    )

    await hyperrag._on_task_callback(task, TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value)

    storage.update_metadata.assert_awaited_once_with(
        "kb-demo",
        "doc-1",
        {"asset_type": "doc", "page_count": 12, "chunk_nums": 37},
    )


@pytest.mark.asyncio
async def test_async_knowledgebase_summary_stores_old_style_single_asset_summary() -> None:
    hyperrag = _build_hyperrag()
    status = _build_status(
        asset_id="mini_lam_pdf_001",
        file_path="/tmp/Foamtec International.pdf",
    )
    hyperrag.aget_knowledgebase_info = AsyncMock(return_value=None)
    hyperrag.alist_assets = AsyncMock(return_value={"mini_lam_pdf_001": status})
    hyperrag._get_kg_experiment = AsyncMock()
    hyperrag.adelete_knowledgebase_info = AsyncMock()

    async def fake_upsert(knowledgebase_id: str, **kwargs):
        return KnowledgebaseInfo(
            id=knowledgebase_id,
            name=kwargs.get("name"),
            created_at=1.0,
            updated_at=2.0,
            asset_ids=kwargs.get("asset_ids") or [],
            summarization=kwargs.get("summarization") or "",
            metadata=kwargs.get("metadata"),
        )

    hyperrag.aupsert_knowledgebase_info = AsyncMock(side_effect=fake_upsert)

    record = await hyperrag.async_knowledgebase_summary(
        "test_kb_001",
        asset_id="mini_lam_pdf_001",
        asset_title="Foamtec International",
        document_summary="本文档介绍LAM 2300腔体预防性维护方案。",
    )

    assert record.asset_ids == ["mini_lam_pdf_001"]
    assert record.summarization == (
        "当前知识库已索引完成1个文档、0个视频。\n"
        "整体概括：本文档介绍LAM 2300腔体预防性维护方案。\n"
        "逐项摘要：\n"
        "1. 【文档】《Foamtec International》：本文档介绍LAM 2300腔体预防性维护方案。"
    )
    assert record.metadata == {
        "asset_summaries": {
            "mini_lam_pdf_001": {
                "title": "Foamtec International",
                "type_label": "文档",
                "summary": "本文档介绍LAM 2300腔体预防性维护方案。",
                "source": "document_summary",
                "status_updated_at": "10.000000",
            }
        },
        "asset_count": 1,
        "overview": "本文档介绍LAM 2300腔体预防性维护方案。",
        "summary_updated_at": record.metadata["summary_updated_at"],
    }


@pytest.mark.asyncio
async def test_async_knowledgebase_summary_keeps_empty_kb_row_instead_of_deleting() -> None:
    hyperrag = _build_hyperrag()
    existing = KnowledgebaseInfo(
        id="test_kb_001",
        name="test_kb_001",
        created_at=1.0,
        updated_at=2.0,
        asset_ids=["mini_lam_pdf_001"],
        summarization="old",
        metadata={"asset_summaries": {"mini_lam_pdf_001": {"summary": "old"}}},
    )
    hyperrag.aget_knowledgebase_info = AsyncMock(return_value=existing)
    hyperrag.alist_assets = AsyncMock(
        return_value={
            "mini_lam_pdf_001": _build_status(
                asset_id="mini_lam_pdf_001",
                status=AssetStatus.DELETED,
            )
        }
    )
    hyperrag.adelete_knowledgebase_info = AsyncMock()
    hyperrag._get_kg_experiment = AsyncMock()

    async def fake_upsert(knowledgebase_id: str, **kwargs):
        return KnowledgebaseInfo(
            id=knowledgebase_id,
            name=kwargs.get("name"),
            created_at=1.0,
            updated_at=2.0,
            asset_ids=kwargs.get("asset_ids") or [],
            summarization=kwargs.get("summarization") or "",
            metadata=kwargs.get("metadata"),
        )

    hyperrag.aupsert_knowledgebase_info = AsyncMock(side_effect=fake_upsert)

    record = await hyperrag.async_knowledgebase_summary(
        "test_kb_001",
        asset_id="mini_lam_pdf_001",
        remove_asset=True,
    )

    assert record.asset_ids == []
    assert record.summarization == EMPTY_KNOWLEDGEBASE_SUMMARY
    hyperrag.adelete_knowledgebase_info.assert_not_called()


@pytest.mark.asyncio
async def test_async_knowledgebase_summary_remove_asset_excludes_deleting_asset_from_counts() -> (
    None
):
    hyperrag = _build_hyperrag()
    existing = KnowledgebaseInfo(
        id="kb-demo",
        name="kb-demo",
        created_at=1.0,
        updated_at=2.0,
        asset_ids=["doc-1", "doc-2"],
        summarization="old",
        metadata={
            "asset_summaries": {
                "doc-1": {
                    "title": "文档一",
                    "type_label": "文档",
                    "summary": "文档一摘要",
                    "status_updated_at": "11.000000",
                },
                "doc-2": {
                    "title": "文档二",
                    "type_label": "文档",
                    "summary": "文档二摘要",
                    "status_updated_at": "12.000000",
                },
            }
        },
    )
    hyperrag.aget_knowledgebase_info = AsyncMock(return_value=existing)
    hyperrag.alist_assets = AsyncMock(
        return_value={
            "doc-1": _build_status(
                asset_id="doc-1",
                status=AssetStatus.DELETING,
                updated_at=13.0,
            ),
            "doc-2": _build_status(asset_id="doc-2", updated_at=12.0),
        }
    )
    extractor = SimpleNamespace(complete_text=AsyncMock(return_value="文档二覆盖关键流程。"))
    experiment = SimpleNamespace(_get_extractor=lambda: extractor)
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)
    hyperrag.adelete_knowledgebase_info = AsyncMock()

    async def fake_upsert(knowledgebase_id: str, **kwargs):
        return KnowledgebaseInfo(
            id=knowledgebase_id,
            name=kwargs.get("name"),
            created_at=1.0,
            updated_at=2.0,
            asset_ids=kwargs.get("asset_ids") or [],
            summarization=kwargs.get("summarization") or "",
            metadata=kwargs.get("metadata"),
        )

    hyperrag.aupsert_knowledgebase_info = AsyncMock(side_effect=fake_upsert)

    record = await hyperrag.async_knowledgebase_summary(
        "kb-demo",
        asset_id="doc-1",
        remove_asset=True,
    )

    assert record.asset_ids == ["doc-2"]
    assert record.metadata["asset_count"] == 1
    assert set(record.metadata["asset_summaries"].keys()) == {"doc-2"}
    hyperrag.adelete_knowledgebase_info.assert_not_called()


@pytest.mark.asyncio
async def test_async_knowledgebase_summary_excludes_deleting_assets_from_active_ids() -> None:
    hyperrag = _build_hyperrag()
    existing = KnowledgebaseInfo(
        id="kb-demo",
        name="kb-demo",
        created_at=1.0,
        updated_at=2.0,
        asset_ids=["doc-1", "doc-2"],
        summarization="old",
        metadata={
            "asset_summaries": {
                "doc-1": {
                    "title": "文档一",
                    "type_label": "文档",
                    "summary": "文档一摘要",
                    "status_updated_at": "11.000000",
                },
                "doc-2": {
                    "title": "文档二",
                    "type_label": "文档",
                    "summary": "文档二摘要",
                    "status_updated_at": "12.000000",
                },
            }
        },
    )
    hyperrag.aget_knowledgebase_info = AsyncMock(return_value=existing)
    hyperrag.alist_assets = AsyncMock(
        return_value={
            "doc-1": _build_status(
                asset_id="doc-1",
                status=AssetStatus.DELETING,
                updated_at=13.0,
            ),
            "doc-2": _build_status(asset_id="doc-2", updated_at=12.0),
        }
    )
    extractor = SimpleNamespace(complete_text=AsyncMock(return_value="文档二覆盖关键流程。"))
    experiment = SimpleNamespace(_get_extractor=lambda: extractor)
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)
    hyperrag.adelete_knowledgebase_info = AsyncMock()

    async def fake_upsert(knowledgebase_id: str, **kwargs):
        return KnowledgebaseInfo(
            id=knowledgebase_id,
            name=kwargs.get("name"),
            created_at=1.0,
            updated_at=2.0,
            asset_ids=kwargs.get("asset_ids") or [],
            summarization=kwargs.get("summarization") or "",
            metadata=kwargs.get("metadata"),
        )

    hyperrag.aupsert_knowledgebase_info = AsyncMock(side_effect=fake_upsert)

    record = await hyperrag.async_knowledgebase_summary(
        "kb-demo",
        asset_id="doc-2",
        asset_title="文档二",
        document_summary="文档二覆盖关键流程。",
    )

    assert record.asset_ids == ["doc-2"]
    assert record.metadata["asset_count"] == 1
    assert set(record.metadata["asset_summaries"].keys()) == {"doc-2"}


@pytest.mark.asyncio
async def test_on_task_callback_marks_delete_index_as_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyperrag = _build_hyperrag()
    status_storage = SimpleNamespace(set_status=AsyncMock())
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: status_storage)

    task = SimpleNamespace(
        workspace="kb-demo",
        asset_id="doc-1",
        task_type=TaskType.DELETE_INDEX,
        job_name="delete",
        error_message=None,
        metadata={},
    )

    await hyperrag._on_task_callback(task, TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value)

    status_storage.set_status.assert_awaited_once_with("kb-demo", "doc-1", AssetStatus.DELETED)


@pytest.mark.asyncio
async def test_async_knowledgebase_summary_composes_multi_asset_summary() -> None:
    hyperrag = _build_hyperrag()
    existing = KnowledgebaseInfo(
        id="kb-demo",
        name="kb-demo",
        created_at=1.0,
        updated_at=2.0,
        asset_ids=["doc-1", "video-1"],
        summarization="",
        metadata={
            "asset_summaries": {
                "doc-1": {
                    "title": "工艺文档",
                    "type_label": "文档",
                    "summary": "介绍刻蚀机工艺参数与维护步骤。",
                    "status_updated_at": "11.000000",
                },
                "video-1": {
                    "title": "维护视频",
                    "type_label": "视频",
                    "summary": "展示标准化拆洗与复位流程。",
                    "status_updated_at": "12.000000",
                },
            }
        },
    )
    hyperrag.aget_knowledgebase_info = AsyncMock(return_value=existing)
    hyperrag.alist_assets = AsyncMock(
        return_value={
            "doc-1": _build_status(asset_id="doc-1", updated_at=11.0, file_path="/tmp/doc-1.pdf"),
            "video-1": _build_status(
                asset_id="video-1",
                parse_mode="video",
                updated_at=12.0,
                file_path="/tmp/video-1.mp4",
            ),
        }
    )
    hyperrag.adelete_knowledgebase_info = AsyncMock()

    extractor = SimpleNamespace(
        complete_text=AsyncMock(
            return_value="覆盖刻蚀机工艺文档与现场维护视频，可查询参数、流程及操作细节。"
        )
    )
    experiment = SimpleNamespace(_get_extractor=lambda: extractor)
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)

    async def fake_upsert(knowledgebase_id: str, **kwargs):
        return KnowledgebaseInfo(
            id=knowledgebase_id,
            name=kwargs.get("name"),
            created_at=1.0,
            updated_at=2.0,
            asset_ids=kwargs.get("asset_ids") or [],
            summarization=kwargs.get("summarization") or "",
            metadata=kwargs.get("metadata"),
        )

    hyperrag.aupsert_knowledgebase_info = AsyncMock(side_effect=fake_upsert)

    record = await hyperrag.async_knowledgebase_summary(
        "kb-demo",
        asset_id="doc-1",
        asset_title="工艺文档",
        document_summary="介绍刻蚀机工艺参数与维护步骤。",
    )

    assert record.summarization == (
        "当前知识库已索引完成1个文档、1个视频。\n"
        "整体概括：覆盖刻蚀机工艺文档与现场维护视频，可查询参数、流程及操作细节。\n"
        "逐项摘要：\n"
        "1. 【文档】《工艺文档》：介绍刻蚀机工艺参数与维护步骤。\n"
        "2. 【视频】《维护视频》：展示标准化拆洗与复位流程。"
    )


@pytest.mark.asyncio
async def test_async_knowledgebase_summary_synchronizes_discovery_catalog(monkeypatch) -> None:
    hyperrag = _build_hyperrag()
    hyperrag._core_initialized = True
    catalog = SimpleNamespace(synchronize_library=AsyncMock())
    hyperrag._knowledge_discovery_catalog_storage = catalog
    status = _build_status(asset_id="doc-1", updated_at=10.0)
    hyperrag.aget_knowledgebase_info = AsyncMock(return_value=None)
    hyperrag.alist_assets = AsyncMock(return_value={"doc-1": status})
    hyperrag._get_kg_experiment = AsyncMock()

    async def fake_upsert(knowledgebase_id: str, **kwargs):
        return KnowledgebaseInfo(
            id=knowledgebase_id,
            name=kwargs.get("name"),
            created_at=1.0,
            updated_at=2.0,
            asset_ids=kwargs.get("asset_ids") or [],
            summarization=kwargs.get("summarization") or "",
            metadata=kwargs.get("metadata"),
        )

    from contextlib import asynccontextmanager

    source = SimpleNamespace(upsert=AsyncMock(side_effect=fake_upsert))
    source.with_db = lambda db: source
    catalog.db = object()

    @asynccontextmanager
    async def transaction(library_id):
        yield catalog

    catalog.library_transaction = transaction
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_knowledgebase_info_storage", lambda: source)

    await hyperrag.async_knowledgebase_summary(
        "kb-demo",
        asset_id="doc-1",
        asset_title="维护文档",
        document_summary="描述清洗流程。",
    )

    catalog.synchronize_library.assert_awaited_once()
    library, assets = catalog.synchronize_library.await_args.args
    assert library.library_id == "kb-demo"
    assert library.asset_count == 1
    assert assets[0].asset_id == "doc-1"
    assert assets[0].title == "维护文档"


@pytest.mark.asyncio
async def test_alint_batch_uses_experiment_lint_batch_size() -> None:
    hyperrag = _build_hyperrag()
    hyperrag.config.lint_batch_size = 17
    hyperrag._ensure_fast_ingest_runtime = AsyncMock(return_value=object())
    experiment = AsyncMock()
    experiment.lint_fast_ingest_post_compile_batch = AsyncMock(return_value={"ok": True})
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)

    result = await hyperrag.alint_batch(workspace="kb-demo")

    assert result == {"ok": True}
    experiment.lint_fast_ingest_post_compile_batch.assert_awaited_once_with(
        workspace="kb-demo",
        batch_size=17,
    )


@pytest.mark.asyncio
async def test_apost_compile_batch_persists_graph_vectors_before_clearing_hot_buffer() -> None:
    hyperrag = _build_hyperrag()
    hyperrag.config.post_compile_batch_size = 5000
    hyperrag.config.fast_ingest_compile_phase = "FAST_INGEST"
    prepared_edges = [{"edge_id": "edge-1"}]
    runtime = AsyncMock()
    runtime.prepare_post_compile_batch = AsyncMock(
        return_value={"batch_id": "batch-1", "edges": prepared_edges}
    )
    runtime.finish_post_compile_batch = AsyncMock(return_value={"removed_edges": 1})
    hyperrag._ensure_fast_ingest_runtime = AsyncMock(return_value=runtime)

    experiment = AsyncMock()
    experiment.repository.merge_fast_ingest_post_compile_edges = AsyncMock(
        return_value={"merged_edges": 1, "merged_entities": 2}
    )
    experiment.upsert_fast_ingest_post_compile_vectors = AsyncMock(
        return_value={"entity_vectors": 2, "relation_vectors": 1}
    )
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)

    result = await hyperrag.apost_compile_batch(workspace="kb-demo")

    assert result["vector_storage"] == {"entity_vectors": 2, "relation_vectors": 1}
    experiment.repository.merge_fast_ingest_post_compile_edges.assert_awaited_once()
    experiment.upsert_fast_ingest_post_compile_vectors.assert_awaited_once_with(prepared_edges)
    runtime.finish_post_compile_batch.assert_awaited_once_with(
        workspace="kb-demo",
        batch_id="batch-1",
        success=True,
    )


@pytest.mark.asyncio
async def test_aauto_post_compile_batch_runs_on_interval_or_hot_buffer_threshold() -> None:
    hyperrag = _build_hyperrag()
    hyperrag.config.post_compile_auto_enabled = True
    hyperrag.config.post_compile_hot_buffer_edge_threshold = 5
    runtime = AsyncMock()
    runtime.unverified_edge_count = AsyncMock(return_value=4)
    hyperrag._ensure_fast_ingest_runtime = AsyncMock(return_value=runtime)
    hyperrag.apost_compile_batch = AsyncMock(return_value={"kg_merged_edges": 1})

    below_threshold = await hyperrag.aauto_post_compile_batch(
        workspace="kb-demo",
        interval_elapsed=False,
    )
    on_interval = await hyperrag.aauto_post_compile_batch(
        workspace="kb-demo",
        interval_elapsed=True,
    )

    assert below_threshold["skipped"] is True
    assert below_threshold["reason"] == "below_hot_buffer_threshold"
    assert on_interval == {"kg_merged_edges": 1}
    hyperrag.apost_compile_batch.assert_awaited_once_with(workspace="kb-demo")


@pytest.mark.asyncio
async def test_lint_issue_api_delegates_to_experiment() -> None:
    hyperrag = _build_hyperrag()
    experiment = AsyncMock()
    experiment.list_lint_issues = AsyncMock(return_value={"issues": [{"issue_id": "i1"}]})
    experiment.delete_lint_issue = AsyncMock(return_value={"deleted": True})
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)

    listed = await hyperrag.alist_lint_issues(
        workspace="kb-demo",
        status="OPEN",
        limit=20,
        offset=5,
    )
    deleted = await hyperrag.adelete_lint_issue(workspace="kb-demo", issue_id="i1")

    assert listed["issues"][0]["issue_id"] == "i1"
    assert deleted["deleted"] is True
    experiment.list_lint_issues.assert_awaited_once_with(
        workspace="kb-demo",
        status="OPEN",
        limit=20,
        offset=5,
    )
    experiment.delete_lint_issue.assert_awaited_once_with(
        workspace="kb-demo",
        issue_id="i1",
    )


@pytest.mark.asyncio
async def test_aauto_lint_batch_respects_time_window() -> None:
    hyperrag = _build_hyperrag()
    hyperrag.config.lint_auto_start_hour = 1
    hyperrag.config.lint_auto_end_hour = 2
    hyperrag.alint_batch = AsyncMock(return_value={"ok": True})

    outside = await hyperrag.aauto_lint_batch(workspace="kb-demo")
    assert outside["skipped"] is True
    assert outside["reason"] == "outside_lint_auto_window"

    hyperrag.config.lint_auto_start_hour = 0
    hyperrag.config.lint_auto_end_hour = 24
    inside = await hyperrag.aauto_lint_batch(workspace="kb-demo")
    assert inside == {"ok": True}
    hyperrag.alint_batch.assert_awaited_once_with(workspace="kb-demo", max_edges=None)


@pytest.mark.asyncio
async def test_adelete_fast_ingest_asset_data_cleans_hot_buffer_and_post_compile_kg() -> None:
    hyperrag = _build_hyperrag()
    runtime = AsyncMock()
    runtime.delete_doc_relations = AsyncMock(return_value={"deleted_edges": 2})
    experiment = AsyncMock()
    experiment.delete_fast_ingest_post_compile_asset = AsyncMock(
        return_value={"deleted_relations": 3}
    )
    hyperrag._ensure_fast_ingest_runtime = AsyncMock(return_value=runtime)
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)

    result = await hyperrag.adelete_fast_ingest_asset_data(
        workspace="kb-demo",
        asset_id="doc-1",
    )

    assert result["hot_buffer"]["deleted_edges"] == 2
    assert result["post_compile_kg"]["deleted_relations"] == 3
    runtime.delete_doc_relations.assert_awaited_once_with(workspace="kb-demo", doc_id="doc-1")
    experiment.delete_fast_ingest_post_compile_asset.assert_awaited_once_with(
        workspace="kb-demo",
        asset_id="doc-1",
    )


@pytest.mark.parametrize("outcome", ["partial_failed", "completed", "completed_with_warnings"])
@pytest.mark.parametrize("source", ["extra", "semantic"])
@pytest.mark.asyncio
async def test_caption_outcome_survives_parse_kg_and_job_completion(monkeypatch, outcome, source):
    """A successful rerun replaces old partial state; KG/job completion preserves it."""
    status = _build_status(asset_id="video-test", parse_mode="video", status=AssetStatus.PARSING)
    status.metadata = {
        "indexing_outcome": "partial_failed",
        "caption_stats": {"failed": 99},
        "global_context_quality_warnings": ["old warning"],
    }
    stats = {
        "total": 3,
        "succeeded": 2 if outcome == "partial_failed" else 3,
        "failed": 1 if outcome == "partial_failed" else 0,
        "failed_nodes": [
            {
                "node_id": "clip-2",
                "retry_error": "injected failure",
                "caption_run_history": [{"round": 1}, {"round": 2}],
            }
        ]
        if outcome == "partial_failed"
        else [],
    }
    warnings = ["观察计划可能过于泛化"] if outcome != "completed" else []
    completeness = {
        "caption_stats": stats,
        "indexing_outcome": outcome,
        "global_context_quality_warnings": warnings,
    }

    async def update_metadata(_workspace, _asset, updates, **_kwargs):
        status.metadata.update(updates)

    async def set_status(_workspace, _asset, value, **_kwargs):
        status.status = value

    storage = SimpleNamespace(
        get_status=AsyncMock(return_value=status),
        update_metadata=AsyncMock(side_effect=update_metadata),
        set_status=AsyncMock(side_effect=set_status),
    )
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: storage)
    hyperrag = _build_hyperrag()
    hyperrag._user_on_job_callback = None
    asset = {
        "modality": "video",
        "extra": {"duration_sec": 30, **(completeness if source == "extra" else {})},
    }
    task = SimpleNamespace(
        workspace="test-workspace",
        asset_id="video-test",
        task_type=TaskType.VIDEO_PARSE,
        metadata={},
        result={"asset": asset, "sop_full": {"metadata": completeness}},
    )
    await hyperrag._on_task_callback(task, "running", "completed")
    assert status.metadata["caption_stats"] == stats
    assert status.metadata["indexing_outcome"] == outcome
    assert status.metadata["global_context_quality_warnings"] == warnings
    task.task_type = TaskType.KG_INDEX
    task.result = {
        "asset": {"modality": "video", "extra": {"duration_sec": 30, **completeness}},
        "counters": {"chunks": stats["succeeded"]},
    }
    await hyperrag._on_task_callback(task, "running", "completed")
    job = SimpleNamespace(
        workspace="test-workspace",
        asset_id="video-test",
        job_name="video",
        metadata={"pre_compile": True},
    )
    await hyperrag._on_job_callback(job, "running", "completed")
    persisted = await hyperrag.aget_asset_status("test-workspace", "video-test")
    assert persisted.status == AssetStatus.COMPLETED
    assert persisted.metadata["caption_stats"] == stats
    assert persisted.metadata["indexing_outcome"] == outcome
    assert persisted.metadata["global_context_quality_warnings"] == warnings
    assert persisted.metadata["chunk_nums"] == stats["succeeded"]
