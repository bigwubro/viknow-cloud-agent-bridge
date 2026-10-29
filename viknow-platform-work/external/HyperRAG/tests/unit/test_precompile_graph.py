from __future__ import annotations

from hyperrag.kg.constants import (
    PRECOMPILED_CHUNK_ID,
    PRECOMPILED_FILE_PATH,
    PRECOMPILED_LIBRARY_ID,
)
from hyperrag.kg.precompile_graph import build_precompiled_import_payload
from hyperrag.single_layer_kg import (
    _dedupe_keep_order,
    _derive_relation_keywords,
    _md5_id,
    _normalize_entity_lookup_key,
)


def test_build_precompiled_import_payload_generates_marked_rows() -> None:
    entity_rows, relation_rows = build_precompiled_import_payload(
        package={
            "entities": [
                {
                    "name": "门机控制器",
                    "type": "Component",
                    "description": "控制门机",
                    "aliases": ["MCU"],
                }
            ],
            "relations": [
                {
                    "source_entity": "门机控制器",
                    "target_entity": "制动单元",
                    "description": "控制器驱动制动单元",
                    "relation_keywords": ["驱动"],
                }
            ],
        },
        company_replacements=None,
        alias_fullname_map=None,
        normalize_entity_lookup_key=_normalize_entity_lookup_key,
        dedupe_keep_order=_dedupe_keep_order,
        derive_relation_keywords=_derive_relation_keywords,
        md5_id=_md5_id,
    )

    assert len(entity_rows) == 2
    assert len(relation_rows) == 1
    assert entity_rows[0]["file_path"] == PRECOMPILED_FILE_PATH
    assert entity_rows[0]["library_id"] == PRECOMPILED_LIBRARY_ID
    assert entity_rows[0]["chunk_ids"] == [PRECOMPILED_CHUNK_ID]
    assert relation_rows[0]["file_path"] == PRECOMPILED_FILE_PATH
    assert relation_rows[0]["library_id"] == PRECOMPILED_LIBRARY_ID
    assert relation_rows[0]["chunk_ids"] == [PRECOMPILED_CHUNK_ID]
    assert relation_rows[0]["source_entity"] == "门机控制器"
