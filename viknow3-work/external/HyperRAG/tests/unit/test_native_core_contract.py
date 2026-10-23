from __future__ import annotations

from types import SimpleNamespace

import hyperrag
import pytest
from hyperrag.asset_status import AssetStatus
from hyperrag.config_loader import HyperRAGConfig
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.single_layer_kg import SingleLayerKGService


def test_package_import_exposes_native_core_only() -> None:
    assert hyperrag.HyperRAGV2 is HyperRAGV2
    assert hyperrag.SingleLayerKGService is not None


def test_core_lifecycle_uses_native_services() -> None:
    runtime = HyperRAGV2(config=HyperRAGConfig())

    assert runtime._kg_experiments == {}


@pytest.mark.asyncio
async def test_workspace_status_uses_native_asset_and_graph_stores(monkeypatch) -> None:
    runtime = HyperRAGV2(config=HyperRAGConfig())

    class StatusStore:
        async def get_all_status(self, workspace: str):
            assert workspace == "native"
            return {
                "asset-1": type("Status", (), {"status": AssetStatus.COMPLETED})(),
                "asset-2": type("Status", (), {"status": AssetStatus.INDEXING})(),
            }

    class Service:
        async def get_workspace_summary(self):
            return {"entities": 3, "relations": 2}

    async def get_service(workspace: str):
        assert workspace == "native"
        return Service()

    async def ensure_core() -> None:
        return None

    monkeypatch.setattr("hyperrag.hyperrag_v2.get_asset_status_storage", lambda: StatusStore())
    monkeypatch.setattr(runtime, "_ensure_core_runtime", ensure_core)
    monkeypatch.setattr(runtime, "_get_kg_experiment", get_service)

    result = await runtime.aget_workspace_status("native")

    assert result == {
        "workspace": "native",
        "asset_count": 2,
        "status_counts": {AssetStatus.COMPLETED.value: 1, AssetStatus.INDEXING.value: 1},
        "graph": {"entities": 3, "relations": 2},
    }


@pytest.mark.asyncio
async def test_entityless_document_delete_removes_chunk_vectors(monkeypatch, tmp_path) -> None:
    file_path = tmp_path / "entityless.md"

    class Repository:
        async def delete_document_graph(self, path: str):
            assert path == str(file_path.resolve())
            raise ValueError(f"Document graph not found: {path}")

    class VectorStore:
        def __init__(self) -> None:
            self.deleted_paths: list[tuple[str, str]] = []

        async def delete_by_file_path(self, *, workspace: str, file_path: str) -> None:
            self.deleted_paths.append((workspace, file_path))

    service = object.__new__(SingleLayerKGService)
    service.config = SimpleNamespace(kg_vector_enabled=True)
    service.workspace = "native"
    service.repository = Repository()
    vector_store = VectorStore()
    monkeypatch.setattr(service, "_get_vector_store", lambda: vector_store)

    with pytest.raises(ValueError, match="Document graph not found"):
        await service.delete_document_graph(str(file_path))

    assert vector_store.deleted_paths == [("native", str(file_path.resolve()))]
