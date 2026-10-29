from __future__ import annotations

from typing import Any

from .constants import (
    PRECOMPILED_CHUNK_ID,
    PRECOMPILED_FILE_PATH,
    PRECOMPILED_LIBRARY_ID,
)


def is_precompiled_chunk_ids(chunk_ids: list[str] | None) -> bool:
    if not chunk_ids:
        return False
    normalized = [str(item).strip() for item in chunk_ids if str(item).strip()]
    if not normalized:
        return False
    return all(item == PRECOMPILED_CHUNK_ID for item in normalized)


def normalize_kg_query_output(
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    default_library_id: str,
) -> dict[str, list[dict[str, str]]]:
    normalized_entities: list[dict[str, str]] = []
    for entity in entities:
        name = str(entity.get("entity_name") or "").strip()
        if not name:
            continue
        file_path = str(entity.get("file_path") or "").strip()
        chunk_ids = [
            str(item).strip() for item in (entity.get("chunk_ids") or []) if str(item).strip()
        ]
        library_id = (
            PRECOMPILED_LIBRARY_ID
            if file_path == PRECOMPILED_FILE_PATH or is_precompiled_chunk_ids(chunk_ids)
            else default_library_id
        )
        normalized_entities.append(
            {
                "name": name,
                "description": str(entity.get("entity_description") or "").strip(),
                "library_id": library_id,
            }
        )

    normalized_relations: list[dict[str, str]] = []
    for relation in relations:
        description = str(relation.get("relationship_description") or "").strip()
        if not description:
            continue
        file_path = str(relation.get("file_path") or "").strip()
        chunk_ids = [
            str(item).strip() for item in (relation.get("chunk_ids") or []) if str(item).strip()
        ]
        library_id = (
            PRECOMPILED_LIBRARY_ID
            if file_path == PRECOMPILED_FILE_PATH or is_precompiled_chunk_ids(chunk_ids)
            else default_library_id
        )
        normalized_relations.append({"description": description, "library_id": library_id})

    return {"entities": normalized_entities, "relations": normalized_relations}

