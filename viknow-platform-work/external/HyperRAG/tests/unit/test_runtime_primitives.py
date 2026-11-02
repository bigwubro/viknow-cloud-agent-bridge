import asyncio
import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).parents[2] / "src" / "hyperrag" / "runtime_primitives.py"
SPEC = importlib.util.spec_from_file_location("runtime_primitives", MODULE_PATH)
assert SPEC and SPEC.loader
PRIMITIVES = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PRIMITIVES
SPEC.loader.exec_module(PRIMITIVES)


def test_storage_primitives_keep_runtime_configuration() -> None:
    config = {"working_dir": "/data"}

    key_value = PRIMITIVES.BaseKVStorage("status", "workspace", config)
    vector = PRIMITIVES.BaseVectorStorage("chunks", "workspace", config, embedding_func="embed")

    assert key_value.global_config is config
    assert vector.embedding_func == "embed"


def test_query_and_sql_primitives_preserve_declared_order() -> None:
    query = PRIMITIVES.QueryParam(mode="local", chunk_top_k=3, max_total_tokens=100)

    assert query.mode == "local"
    assert query.chunk_top_k == 3
    assert query.max_total_tokens == 100
    assert PRIMITIVES._normalize_sql_params({"$2": "second", "$1": "first"}) == [
        "first",
        "second",
    ]


def test_namespace_locks_are_scoped_and_reused() -> None:
    first = PRIMITIVES.get_namespace_lock("chunks", "workspace-a")

    assert first is PRIMITIVES.get_namespace_lock("chunks", "workspace-a")
    assert first is not PRIMITIVES.get_namespace_lock("chunks", "workspace-b")
    assert isinstance(first, asyncio.Lock)


def test_reference_generation_preserves_chunks_and_groups_file_paths() -> None:
    chunks = [
        {"file_path": "manual.pdf", "content": "first"},
        {"metadata": {"file_path": "manual.pdf"}, "content": "second"},
        {"file_path": "unknown_source", "content": "third"},
    ]

    references, updated_chunks = PRIMITIVES.generate_reference_list_from_chunks(chunks)

    assert references == [{"reference_id": "1", "file_path": "manual.pdf"}]
    assert [chunk.get("reference_id") for chunk in updated_chunks] == ["1", "1", None]
    assert "reference_id" not in chunks[0]
