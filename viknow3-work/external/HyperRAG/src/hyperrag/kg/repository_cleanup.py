from __future__ import annotations

from typing import Any


async def clear_workspace(
    *,
    driver: Any,
    database: str | None,
    workspace: str,
    entity_label: str,
    relation_type: str,
    lint_issue_label: str,
) -> dict[str, Any]:
    async with driver.session(database=database) as session:
        result = await session.run(
            f"""
            MATCH (n:{entity_label} {{workspace: $workspace}})
            OPTIONAL MATCH (n)-[r:{relation_type}]-()
            OPTIONAL MATCH (i:{lint_issue_label} {{workspace: $workspace}})
            RETURN count(DISTINCT n) AS entities,
                   count(DISTINCT r) AS relations,
                   count(DISTINCT i) AS issues
            """,
            workspace=workspace,
        )
        summary = await result.single()
        await session.run(
            f"MATCH (n:{entity_label} {{workspace: $workspace}}) DETACH DELETE n",
            workspace=workspace,
        )
        await session.run(
            f"MATCH (i:{lint_issue_label} {{workspace: $workspace}}) DETACH DELETE i",
            workspace=workspace,
        )
    return {
        "workspace": workspace,
        "deleted": True,
        "entities": int((summary or {}).get("entities") or 0),
        "relations": int((summary or {}).get("relations") or 0),
        "issues": int((summary or {}).get("issues") or 0),
    }


async def clear_workspaces_by_prefix(
    *,
    driver: Any,
    database: str | None,
    workspace_prefix: str,
    entity_label: str,
    relation_type: str,
    lint_issue_label: str,
) -> dict[str, Any]:
    async with driver.session(database=database) as session:
        result = await session.run(
            f"""
            MATCH (n:{entity_label})
            WHERE n.workspace STARTS WITH $workspace_prefix
            OPTIONAL MATCH (n)-[r:{relation_type}]-()
            OPTIONAL MATCH (i:{lint_issue_label})
            WHERE i.workspace STARTS WITH $workspace_prefix
            RETURN collect(DISTINCT n.workspace) AS workspaces,
                   count(DISTINCT n) AS entities,
                   count(DISTINCT r) AS relations,
                   count(DISTINCT i) AS issues
            """,
            workspace_prefix=workspace_prefix,
        )
        summary = await result.single()
        await session.run(
            (
                f"MATCH (n:{entity_label}) "
                "WHERE n.workspace STARTS WITH $workspace_prefix DETACH DELETE n"
            ),
            workspace_prefix=workspace_prefix,
        )
        await session.run(
            (
                f"MATCH (i:{lint_issue_label}) "
                "WHERE i.workspace STARTS WITH $workspace_prefix DETACH DELETE i"
            ),
            workspace_prefix=workspace_prefix,
        )
    return {
        "workspace_prefix": workspace_prefix,
        "workspaces": (summary or {}).get("workspaces") or [],
        "deleted": True,
        "entities": int((summary or {}).get("entities") or 0),
        "relations": int((summary or {}).get("relations") or 0),
        "issues": int((summary or {}).get("issues") or 0),
    }
