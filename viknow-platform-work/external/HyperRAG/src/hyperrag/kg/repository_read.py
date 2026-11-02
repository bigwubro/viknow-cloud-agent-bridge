from __future__ import annotations

from typing import Any


async def show_document_graph(
    *,
    driver: Any,
    database: str | None,
    workspace: str,
    file_path: str,
    entity_label: str,
    relation_type: str,
) -> dict[str, Any]:
    async with driver.session(database=database) as session:
        result = await session.run(
            f"""
            MATCH (n:{entity_label} {{workspace: $workspace, file_path: $file_path}})
            OPTIONAL MATCH (n)-[r:{relation_type}]->(m:{entity_label})
            RETURN [item IN collect(DISTINCT n {{
                .node_id, .entity_name, .entity_type, .entity_description, .file_path,
                .status, .chunk_ids, .aliases, .normalized_name, .alias_keys
            }}) WHERE item IS NOT NULL] AS entities,
                   [item IN collect(DISTINCT CASE WHEN r IS NULL THEN NULL ELSE {{
                       relation_id: r.relation_id,
                       source_entity: startNode(r).entity_name,
                       target_entity: endNode(r).entity_name,
                       relationship_description: r.relationship_description,
                       situation: r.situation,
                       relation_keywords: r.relation_keywords,
                       synonym: r.synonym,
                       file_path: r.file_path,
                       chunk_ids: r.chunk_ids
                   }} END) WHERE item IS NOT NULL] AS relations
            """,
            workspace=workspace,
            file_path=file_path,
        )
        row = await result.single()
        if row is None:
            raise ValueError(f"Document graph not found: {file_path}")
        entities = row.get("entities") or []
        if not entities:
            raise ValueError(f"Document graph not found: {file_path}")
        return {
            "file_path": file_path,
            "entities": entities,
            "relations": row.get("relations") or [],
        }


async def show_synonyms(
    *,
    driver: Any,
    database: str | None,
    workspace: str,
    file_path: str | None = None,
    limit: int = 50,
    entity_label: str,
    relation_type: str,
) -> dict[str, Any]:
    async with driver.session(database=database) as session:
        result = await session.run(
            f"""
            MATCH (src:{entity_label} {{workspace: $workspace}})
                  -[r:{relation_type} {{synonym: true}}]->
                  (tgt:{entity_label} {{workspace: $workspace}})
            WHERE $file_path IS NULL OR src.file_path = $file_path OR tgt.file_path = $file_path
            RETURN src.entity_name AS source_entity_name,
                   src.file_path AS source_file_path,
                   tgt.entity_name AS target_entity_name,
                   tgt.file_path AS target_file_path,
                   r.relationship_description AS relationship_description,
                   r.situation AS situation,
                   r.chunk_ids AS chunk_ids
            LIMIT $limit
            """,
            workspace=workspace,
            file_path=file_path,
            limit=limit,
        )
        rows = [record.data() async for record in result]
    return {"workspace": workspace, "file_path": file_path, "synonyms": rows}


async def show_graph_summary(
    *,
    driver: Any,
    database: str | None,
    workspace: str,
    entity_label: str,
    relation_type: str,
) -> dict[str, Any]:
    async with driver.session(database=database) as session:
        result = await session.run(
            f"""
            MATCH (n:{entity_label} {{workspace: $workspace}})
            OPTIONAL MATCH (n)-[r:{relation_type}]->()
            RETURN count(DISTINCT n) AS entity_count,
                   count(DISTINCT CASE WHEN r.synonym THEN r END) AS synonym_relation_count,
                   count(
                       DISTINCT CASE
                           WHEN r.synonym = false OR r.synonym IS NULL THEN r
                       END
                   ) AS ordinary_relation_count,
                   count(DISTINCT n.file_path) AS document_count
            """,
            workspace=workspace,
        )
        row = await result.single()
    return {
        "workspace": workspace,
        "entity_count": int((row or {}).get("entity_count") or 0),
        "ordinary_relation_count": int((row or {}).get("ordinary_relation_count") or 0),
        "synonym_relation_count": int((row or {}).get("synonym_relation_count") or 0),
        "document_count": int((row or {}).get("document_count") or 0),
    }
