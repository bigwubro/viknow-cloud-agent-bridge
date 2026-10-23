from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .constants import PRECOMPILED_CHUNK_ID, PRECOMPILED_FILE_PATH, PRECOMPILED_LIBRARY_ID
from .package import sanitize_precompile_import_package


def build_precompiled_import_payload(
    *,
    package: dict[str, Any],
    company_replacements: dict[str, str] | None,
    alias_fullname_map: dict[str, str] | None,
    normalize_entity_lookup_key: Callable[[str], str],
    dedupe_keep_order: Callable[[list[str]], list[str]],
    derive_relation_keywords: Callable[..., list[str]],
    md5_id: Callable[..., str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entities_payload, relations_payload = sanitize_precompile_import_package(
        package,
        company_replacements=company_replacements,
        alias_fullname_map=alias_fullname_map,
    )

    entity_map: dict[str, dict[str, Any]] = {}
    relation_rows: list[dict[str, Any]] = []

    for item in entities_payload:
        entity_name = str(item.get("name") or "").strip()
        if not entity_name:
            continue
        entity_type = str(item.get("type") or "Concept").strip() or "Concept"
        entity_description = str(item.get("description") or "").strip()
        aliases = dedupe_keep_order(
            [str(alias).strip() for alias in (item.get("aliases") or []) if str(alias).strip()]
        )
        normalized_name = normalize_entity_lookup_key(entity_name)
        alias_keys = dedupe_keep_order([normalize_entity_lookup_key(alias) for alias in aliases])
        entity_map[entity_name.lower()] = {
            "node_id": md5_id(
                "ent-",
                PRECOMPILED_FILE_PATH,
                entity_type,
                entity_name,
            ),
            "entity_name": entity_name,
            "entity_type": entity_type,
            "entity_description": entity_description,
            "file_path": PRECOMPILED_FILE_PATH,
            "status": "PENDING_LINT",
            "library_id": PRECOMPILED_LIBRARY_ID,
            "chunk_ids": [PRECOMPILED_CHUNK_ID],
            "aliases": aliases,
            "normalized_name": normalized_name,
            "alias_keys": alias_keys,
        }

    for item in relations_payload:
        source_entity = str(item.get("source_entity") or "").strip()
        target_entity = str(item.get("target_entity") or "").strip()
        relationship_description = str(item.get("description") or "").strip()
        if not source_entity or not target_entity or not relationship_description:
            continue

        if source_entity.lower() not in entity_map:
            entity_map[source_entity.lower()] = {
                "node_id": md5_id("ent-", PRECOMPILED_FILE_PATH, "Concept", source_entity),
                "entity_name": source_entity,
                "entity_type": "Concept",
                "entity_description": "",
                "file_path": PRECOMPILED_FILE_PATH,
                "status": "PENDING_LINT",
                "library_id": PRECOMPILED_LIBRARY_ID,
                "chunk_ids": [PRECOMPILED_CHUNK_ID],
                "aliases": [],
                "normalized_name": normalize_entity_lookup_key(source_entity),
                "alias_keys": [],
            }
        if target_entity.lower() not in entity_map:
            entity_map[target_entity.lower()] = {
                "node_id": md5_id("ent-", PRECOMPILED_FILE_PATH, "Concept", target_entity),
                "entity_name": target_entity,
                "entity_type": "Concept",
                "entity_description": "",
                "file_path": PRECOMPILED_FILE_PATH,
                "status": "PENDING_LINT",
                "library_id": PRECOMPILED_LIBRARY_ID,
                "chunk_ids": [PRECOMPILED_CHUNK_ID],
                "aliases": [],
                "normalized_name": normalize_entity_lookup_key(target_entity),
                "alias_keys": [],
            }

        situation = str(item.get("situation") or "").strip()
        relation_rows.append(
            {
                "relation_id": md5_id(
                    "rel-",
                    PRECOMPILED_FILE_PATH,
                    source_entity,
                    target_entity,
                    relationship_description,
                    situation,
                ),
                "source_entity": source_entity,
                "target_entity": target_entity,
                "relationship_description": relationship_description,
                "situation": situation,
                "relation_keywords": derive_relation_keywords(
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relationship_description=relationship_description,
                    situation=situation,
                    raw_keywords=item.get("relation_keywords") or [],
                ),
                "synonym": bool(item.get("synonym", False)),
                "file_path": PRECOMPILED_FILE_PATH,
                "status": "PENDING_LINT",
                "library_id": PRECOMPILED_LIBRARY_ID,
                "chunk_ids": [PRECOMPILED_CHUNK_ID],
            }
        )

    return list(entity_map.values()), relation_rows
