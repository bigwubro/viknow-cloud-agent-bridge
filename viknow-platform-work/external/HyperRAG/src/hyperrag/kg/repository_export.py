from __future__ import annotations

from typing import Any


async def export_graph_records(
    *,
    driver: Any,
    database: str | None,
    entity_label: str,
    relation_type: str,
    workspaces: list[str] | None,
) -> dict[str, Any]:
    normalized_workspaces = [str(item).strip() for item in (workspaces or []) if str(item).strip()]
    include_all = not normalized_workspaces
    async with driver.session(database=database) as session:
        entity_result = await session.run(
            f"""
            MATCH (n:{entity_label})
            WHERE $include_all OR n.workspace IN $workspaces
            RETURN collect(DISTINCT n {{
                .workspace,
                .node_id,
                .entity_name,
                .entity_type,
                .entity_description,
                .file_path,
                .status,
                .library_id,
                .chunk_ids,
                .aliases,
                .normalized_name,
                .alias_keys,
                .created_at
            }}) AS entities
            """,
            include_all=include_all,
            workspaces=normalized_workspaces,
        )
        entity_row = await entity_result.single()
        relation_result = await session.run(
            f"""
            MATCH (src:{entity_label})-[r:{relation_type}]->(tgt:{entity_label})
            WHERE $include_all OR r.workspace IN $workspaces
            RETURN collect(DISTINCT {{
                workspace: r.workspace,
                relation_id: r.relation_id,
                source_entity: coalesce(r.source_entity, src.entity_name),
                target_entity: coalesce(r.target_entity, tgt.entity_name),
                relationship_description: r.relationship_description,
                situation: coalesce(r.situation, ''),
                relation_keywords: coalesce(r.relation_keywords, []),
                synonym: coalesce(r.synonym, false),
                file_path: coalesce(r.file_path, ''),
                status: coalesce(r.status, ''),
                library_id: coalesce(r.library_id, ''),
                chunk_ids: coalesce(r.chunk_ids, []),
                created_at: r.created_at
            }}) AS relations
            """,
            include_all=include_all,
            workspaces=normalized_workspaces,
        )
        relation_row = await relation_result.single()
    return {
        "workspaces": normalized_workspaces,
        "entities": (entity_row or {}).get("entities") or [],
        "relations": (relation_row or {}).get("relations") or [],
    }
