from __future__ import annotations

import datetime as dt
from typing import Any


def _normalize_mapping(raw: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        src = str(key or "").strip()
        dst = str(value or "").strip()
        if not src or not dst:
            continue
        out[src] = dst
    return out


def _replace_by_mapping(text: str, mapping: dict[str, str]) -> str:
    if not text or not mapping:
        return text
    updated = str(text)
    for src in sorted(mapping.keys(), key=len, reverse=True):
        updated = updated.replace(src, mapping[src])
    return updated


def apply_desensitization(
    text: str,
    *,
    alias_fullname_map: dict[str, str] | None = None,
    company_replacements: dict[str, str] | None = None,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    alias_map = _normalize_mapping(alias_fullname_map)
    company_map = _normalize_mapping(company_replacements)
    normalized = _replace_by_mapping(normalized, alias_map)
    normalized = _replace_by_mapping(normalized, company_map)
    return normalized.strip()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(value)
    return out


def _merge_description(left: str, right: str) -> str:
    l_text = str(left or "").strip()
    r_text = str(right or "").strip()
    if not l_text:
        return r_text
    if not r_text:
        return l_text
    if l_text == r_text:
        return l_text
    return f"{l_text}\n{r_text}"


def build_precompile_export_package(
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    company_replacements: dict[str, str] | None = None,
    alias_fullname_map: dict[str, str] | None = None,
    workspaces: list[str] | None = None,
) -> dict[str, Any]:
    entity_map: dict[tuple[str, str], dict[str, Any]] = {}
    relation_map: dict[tuple[str, str, str, str, bool], dict[str, Any]] = {}

    for row in entities or []:
        name = apply_desensitization(
            str(row.get("entity_name") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        entity_type = apply_desensitization(
            str(row.get("entity_type") or "Concept"),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        ) or "Concept"
        description = apply_desensitization(
            str(row.get("entity_description") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        aliases = [
            apply_desensitization(
                str(alias or ""),
                alias_fullname_map=alias_fullname_map,
                company_replacements=company_replacements,
            )
            for alias in (row.get("aliases") or [])
        ]
        key = (name.lower(), entity_type.lower())
        if not name:
            continue
        current = entity_map.get(key)
        if current is None:
            entity_map[key] = {
                "name": name,
                "type": entity_type,
                "description": description,
                "aliases": _dedupe_keep_order(aliases),
            }
        else:
            current["description"] = _merge_description(current.get("description", ""), description)
            current["aliases"] = _dedupe_keep_order((current.get("aliases") or []) + aliases)

    for row in relations or []:
        source = apply_desensitization(
            str(row.get("source_entity") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        target = apply_desensitization(
            str(row.get("target_entity") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        description = apply_desensitization(
            str(row.get("relationship_description") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        situation = apply_desensitization(
            str(row.get("situation") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        keywords = [
            apply_desensitization(
                str(keyword or ""),
                alias_fullname_map=alias_fullname_map,
                company_replacements=company_replacements,
            )
            for keyword in (row.get("relation_keywords") or [])
        ]
        synonym = bool(row.get("synonym", False))
        key = (
            source.lower(),
            target.lower(),
            description.lower(),
            situation.lower(),
            synonym,
        )
        if not source or not target or not description:
            continue
        existing = relation_map.get(key)
        if existing is None:
            relation_map[key] = {
                "source_entity": source,
                "target_entity": target,
                "description": description,
                "situation": situation,
                "relation_keywords": _dedupe_keep_order(keywords),
                "synonym": synonym,
            }
        else:
            existing["relation_keywords"] = _dedupe_keep_order(
                (existing.get("relation_keywords") or []) + keywords
            )

    return {
        "version": "1.0",
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "entities": list(entity_map.values()),
        "relations": list(relation_map.values()),
        "metadata": {
            "workspaces": [str(item) for item in (workspaces or []) if str(item).strip()],
            "stripped_fields": ["workspace", "file_path", "library_id", "chunk_ids", "source_refs"],
            "desensitized": {
                "company_replacements_count": len(_normalize_mapping(company_replacements)),
                "alias_fullname_map_count": len(_normalize_mapping(alias_fullname_map)),
            },
        },
    }


def sanitize_precompile_import_package(
    package: dict[str, Any],
    *,
    company_replacements: dict[str, str] | None = None,
    alias_fullname_map: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

    for row in package.get("entities") or []:
        if not isinstance(row, dict):
            continue
        name = apply_desensitization(
            str(row.get("name") or row.get("entity_name") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        if not name:
            continue
        entities.append(
            {
                "name": name,
                "type": apply_desensitization(
                    str(row.get("type") or row.get("entity_type") or "Concept"),
                    alias_fullname_map=alias_fullname_map,
                    company_replacements=company_replacements,
                )
                or "Concept",
                "description": apply_desensitization(
                    str(row.get("description") or row.get("entity_description") or ""),
                    alias_fullname_map=alias_fullname_map,
                    company_replacements=company_replacements,
                ),
                "aliases": _dedupe_keep_order(
                    [
                        apply_desensitization(
                            str(alias or ""),
                            alias_fullname_map=alias_fullname_map,
                            company_replacements=company_replacements,
                        )
                        for alias in (row.get("aliases") or [])
                    ]
                ),
            }
        )

    for row in package.get("relations") or []:
        if not isinstance(row, dict):
            continue
        source = apply_desensitization(
            str(row.get("source_entity") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        target = apply_desensitization(
            str(row.get("target_entity") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        description = apply_desensitization(
            str(row.get("description") or row.get("relationship_description") or ""),
            alias_fullname_map=alias_fullname_map,
            company_replacements=company_replacements,
        )
        if not source or not target or not description:
            continue
        relations.append(
            {
                "source_entity": source,
                "target_entity": target,
                "description": description,
                "situation": apply_desensitization(
                    str(row.get("situation") or ""),
                    alias_fullname_map=alias_fullname_map,
                    company_replacements=company_replacements,
                ),
                "relation_keywords": _dedupe_keep_order(
                    [
                        apply_desensitization(
                            str(keyword or ""),
                            alias_fullname_map=alias_fullname_map,
                            company_replacements=company_replacements,
                        )
                        for keyword in (row.get("relation_keywords") or [])
                    ]
                ),
                "synonym": bool(row.get("synonym", False)),
            }
        )

    return entities, relations
