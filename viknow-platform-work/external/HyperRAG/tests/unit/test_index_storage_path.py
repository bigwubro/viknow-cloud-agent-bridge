from pathlib import Path

from hyperrag.single_layer_kg import index_storage_path


def test_relative_gateway_key_is_not_prefixed_with_cwd():
    relative = "knowledge/assets/asset-1/upload/asset-1.pdf"
    assert index_storage_path(relative) == relative
    assert index_storage_path(f"  {relative}  ") == relative
    assert not Path(index_storage_path(relative)).is_absolute()


def test_absolute_path_still_resolves_dot_segments():
    resolved = index_storage_path("/data/viknow/knowledge/../files/doc.pdf")
    assert resolved == "/data/viknow/files/doc.pdf"


def test_empty_path_stays_empty():
    assert index_storage_path("") == ""
    assert index_storage_path("   ") == ""
