"""Deterministic ID recomputation for migration.

HyperRAG derives all graph/vector identifiers from ``file_path`` via MD5:

- entity ``node_id`` = ``ent-`` + md5(file_path, entity_type, entity_name)
- relation id = ``rel-`` + md5(file_path, source_entity, target_entity)
- chunk id = ``chunk-`` + md5(file_path, order, text[:512])
- extraction ids = ``entx-``/``relx-`` + md5(file_path, chunk_id, key)

Because the imported absolute path differs from the source, every derived id
must be recomputed from the target absolute path so ids stay consistent with
the target environment's own generation rule. These helpers mirror HyperRAG's
``single_layer_kg._md5_id`` exactly.
"""

from __future__ import annotations

import hashlib

_CHUNK_TEXT_PREFIX_CHARS = 512


def md5_id(prefix: str, *parts: str) -> str:
    """Reproduce HyperRAG ``_md5_id``: md5 of ``::``-joined parts."""
    digest = hashlib.md5("::".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def recompute_chunk_id(*, file_path: str, order: int, text: str) -> str:
    return md5_id("chunk-", file_path, str(order), text[:_CHUNK_TEXT_PREFIX_CHARS])


def recompute_entity_node_id(*, file_path: str, entity_type: str, entity_name: str) -> str:
    return md5_id("ent-", file_path, entity_type, entity_name)


def recompute_entity_extraction_id(*, file_path: str, chunk_id: str, entity_key: str) -> str:
    return md5_id("entx-", file_path, chunk_id, entity_key)


def recompute_relation_extraction_id(*, file_path: str, chunk_id: str, relation_key: str) -> str:
    return md5_id("relx-", file_path, chunk_id, relation_key)


def recompute_relation_id(*, file_path: str, source_entity: str, target_entity: str) -> str:
    return md5_id("rel-", file_path, source_entity, target_entity)
