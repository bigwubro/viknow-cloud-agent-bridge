"""Persistent lexical catalog used to discover HyperRAG knowledge libraries."""

from __future__ import annotations

import datetime
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import Any, final

import asyncpg

from .discovery_tokens import (
    discovery_token_version,
    discovery_tokens,
    normalize_search_text,
    query_terms,
)
from .runtime_primitives import BaseKVStorage, ClientManager, PostgreSQLDB, get_data_init_lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgeDiscoveryLibrary:
    library_id: str
    title: str
    overview: str
    asset_count: int
    document_count: int
    video_count: int
    normalized_search_text: str
    updated_at: float
    search_tokens: tuple[str, ...] | None = None
    search_version: str = field(default_factory=discovery_token_version)

    def __post_init__(self) -> None:
        if self.search_tokens is None:
            object.__setattr__(
                self,
                "search_tokens",
                tuple(
                    sorted(
                        discovery_tokens(
                            f"{self.title} {self.overview}",
                        )
                    )
                ),
            )


@dataclass(frozen=True)
class KnowledgeDiscoveryAsset:
    library_id: str
    asset_id: str
    title: str
    asset_type: str
    summary: str
    normalized_search_text: str
    is_active: bool
    status_updated_at: float | None
    updated_at: float
    search_tokens: tuple[str, ...] | None = None
    search_version: str = field(default_factory=discovery_token_version)

    def __post_init__(self) -> None:
        if self.search_tokens is None:
            object.__setattr__(
                self,
                "search_tokens",
                tuple(
                    sorted(
                        discovery_tokens(
                            f"{self.title} {self.asset_type} {self.summary}",
                        )
                    )
                ),
            )


def _catalog_search_sql(*, assets: bool = False, ids_only: bool = False) -> str:
    table = (
        "hyperrag_knowledge_discovery_assets"
        if assets
        else "hyperrag_knowledge_discovery_libraries"
    )
    keys = "library_id, asset_id" if assets else "library_id"
    restriction = " AND is_active = TRUE" if assets else ""
    if assets and not ids_only:
        restriction += " AND ($5::text IS NULL OR library_id = $5)"
    else:
        restriction += " AND ($5::text[] IS NULL OR library_id = ANY($5))"
    rank = """CASE WHEN lower(t.title) = $1 THEN 0
              WHEN lower(t.title) LIKE $1 || '%' THEN 1
              WHEN lower(t.title) LIKE '%' || $1 || '%' THEN 2 ELSE 3 END"""
    selection = "t.library_id" if ids_only else "t.*"
    grouping = "GROUP BY t.library_id" if ids_only else ""
    ordering = (
        f"MIN({rank}), MAX(t.updated_at) DESC, t.library_id"
        if ids_only
        else f"{rank}, t.updated_at DESC, t.library_id"
    )
    if assets and not ids_only:
        ordering += ", t.asset_id"
    return f"""
        WITH matched AS (
            SELECT {keys} FROM {table} WHERE search_tokens && $2::text[] {restriction}
            UNION
            SELECT {keys} FROM {table}
            WHERE normalized_search_text LIKE ANY($3::text[]) {restriction}
        )
        SELECT {selection} FROM {table} t JOIN matched USING ({keys})
        {grouping} ORDER BY {ordering} LIMIT NULLIF($4, 0)
    """


SQL_TEMPLATES_KNOWLEDGE_DISCOVERY = {
    "create_trigram_extension": "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public",
    "create_libraries_table": """
        CREATE TABLE IF NOT EXISTS hyperrag_knowledge_discovery_libraries (
            library_id VARCHAR(255) PRIMARY KEY,
            title TEXT NOT NULL,
            overview TEXT NOT NULL DEFAULT '',
            asset_count INTEGER NOT NULL DEFAULT 0,
            document_count INTEGER NOT NULL DEFAULT 0,
            video_count INTEGER NOT NULL DEFAULT 0,
            normalized_search_text TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP NOT NULL,
            search_tokens TEXT[] NOT NULL,
            search_version TEXT NOT NULL
        )
    """,
    "create_assets_table": """
        CREATE TABLE IF NOT EXISTS hyperrag_knowledge_discovery_assets (
            library_id VARCHAR(255) NOT NULL,
            asset_id VARCHAR(500) NOT NULL,
            title TEXT NOT NULL,
            asset_type VARCHAR(100) NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            normalized_search_text TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            status_updated_at TIMESTAMP NULL,
            updated_at TIMESTAMP NOT NULL,
            search_tokens TEXT[] NOT NULL,
            search_version TEXT NOT NULL,
            PRIMARY KEY (library_id, asset_id),
            CONSTRAINT hyperrag_knowledge_discovery_assets_library_fk
                FOREIGN KEY (library_id)
                REFERENCES hyperrag_knowledge_discovery_libraries(library_id)
                ON DELETE CASCADE
        )
    """,
    "create_indexes": [
        """CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_libraries_tokens
        ON hyperrag_knowledge_discovery_libraries USING GIN (search_tokens)""",
        """CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_assets_tokens
        ON hyperrag_knowledge_discovery_assets USING GIN (search_tokens)""",
        """
        CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_libraries_updated
        ON hyperrag_knowledge_discovery_libraries(updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_assets_library_active
        ON hyperrag_knowledge_discovery_assets(library_id, is_active, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_assets_active_updated
        ON hyperrag_knowledge_discovery_assets(is_active, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_libraries_search
        ON hyperrag_knowledge_discovery_libraries
        USING GIN (normalized_search_text gin_trgm_ops)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hyperrag_discovery_assets_search
        ON hyperrag_knowledge_discovery_assets
        USING GIN (normalized_search_text gin_trgm_ops)
        """,
    ],
    "upsert_library": """
        INSERT INTO hyperrag_knowledge_discovery_libraries
            (library_id, title, overview, asset_count, document_count, video_count,
             normalized_search_text, updated_at, search_tokens, search_version)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (library_id) DO UPDATE SET
            title = EXCLUDED.title,
            overview = EXCLUDED.overview,
            asset_count = EXCLUDED.asset_count,
            document_count = EXCLUDED.document_count,
            video_count = EXCLUDED.video_count,
            normalized_search_text = EXCLUDED.normalized_search_text,
            updated_at = EXCLUDED.updated_at,
            search_tokens = EXCLUDED.search_tokens,
            search_version = EXCLUDED.search_version
        WHERE (hyperrag_knowledge_discovery_libraries.title,
               hyperrag_knowledge_discovery_libraries.overview,
               hyperrag_knowledge_discovery_libraries.asset_count,
               hyperrag_knowledge_discovery_libraries.document_count,
               hyperrag_knowledge_discovery_libraries.video_count,
               hyperrag_knowledge_discovery_libraries.normalized_search_text,
               hyperrag_knowledge_discovery_libraries.search_tokens,
               hyperrag_knowledge_discovery_libraries.search_version)
          IS DISTINCT FROM
              (EXCLUDED.title, EXCLUDED.overview, EXCLUDED.asset_count,
               EXCLUDED.document_count, EXCLUDED.video_count, EXCLUDED.normalized_search_text,
               EXCLUDED.search_tokens, EXCLUDED.search_version)
    """,
    "upsert_asset": """
        INSERT INTO hyperrag_knowledge_discovery_assets
            (library_id, asset_id, title, asset_type, summary, normalized_search_text,
             is_active, status_updated_at, updated_at, search_tokens, search_version)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (library_id, asset_id) DO UPDATE SET
            title = EXCLUDED.title,
            asset_type = EXCLUDED.asset_type,
            summary = EXCLUDED.summary,
            normalized_search_text = EXCLUDED.normalized_search_text,
            is_active = EXCLUDED.is_active,
            status_updated_at = EXCLUDED.status_updated_at,
            updated_at = EXCLUDED.updated_at,
            search_tokens = EXCLUDED.search_tokens,
            search_version = EXCLUDED.search_version
        WHERE (hyperrag_knowledge_discovery_assets.title,
               hyperrag_knowledge_discovery_assets.asset_type,
               hyperrag_knowledge_discovery_assets.summary,
               hyperrag_knowledge_discovery_assets.normalized_search_text,
               hyperrag_knowledge_discovery_assets.is_active,
               hyperrag_knowledge_discovery_assets.status_updated_at,
               hyperrag_knowledge_discovery_assets.search_tokens,
               hyperrag_knowledge_discovery_assets.search_version)
          IS DISTINCT FROM
              (EXCLUDED.title, EXCLUDED.asset_type, EXCLUDED.summary,
               EXCLUDED.normalized_search_text, EXCLUDED.is_active, EXCLUDED.status_updated_at,
               EXCLUDED.search_tokens, EXCLUDED.search_version)
    """,
    "get_library": """
        SELECT * FROM hyperrag_knowledge_discovery_libraries WHERE library_id = $1
    """,
    "list_libraries": """
        SELECT * FROM hyperrag_knowledge_discovery_libraries
        ORDER BY updated_at DESC, library_id ASC
    """,
    "get_asset": """
        SELECT * FROM hyperrag_knowledge_discovery_assets
        WHERE library_id = $1 AND asset_id = $2
    """,
    "list_assets": """
        SELECT * FROM hyperrag_knowledge_discovery_assets
        WHERE library_id = $1
        ORDER BY is_active DESC, updated_at DESC, asset_id ASC
    """,
    "list_all_assets": "SELECT * FROM hyperrag_knowledge_discovery_assets",
    "deactivate_missing_assets": """
        UPDATE hyperrag_knowledge_discovery_assets
        SET is_active = FALSE, updated_at = $3
        WHERE library_id = $1 AND NOT (asset_id = ANY($2::text[]))
          AND is_active = TRUE
    """,
    "delete_library": """
        DELETE FROM hyperrag_knowledge_discovery_libraries WHERE library_id = $1
    """,
    "delete_asset": """
        DELETE FROM hyperrag_knowledge_discovery_assets
        WHERE library_id = $1 AND asset_id = $2
    """,
    "search_libraries": _catalog_search_sql(),
    "search_assets": _catalog_search_sql(assets=True),
    "search_asset_library_ids": _catalog_search_sql(assets=True, ids_only=True),
}


@final
@dataclass
class KnowledgeDiscoveryCatalogStorage(BaseKVStorage):
    """PostgreSQL-backed library and asset catalog for deterministic lexical discovery."""

    db: PostgreSQLDB | None = field(default=None)

    def __post_init__(self) -> None:
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with get_data_init_lock():
            if self._initialized:
                return
            if self.db is None:
                self.db = await ClientManager.get_client()
            async with self.db.transaction() as db:
                await db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    ["hyperrag:discovery:schema"],
                )
                await replace(self, db=db)._initialize_schema()
            self._initialized = True
        logger.info("KnowledgeDiscoveryCatalogStorage initialized")

    async def _initialize_schema(self) -> None:
        existing = await self.db.query(
            "SELECT to_regclass(format('%I.hyperrag_knowledge_discovery_libraries', "
            "current_schema())) IS NOT NULL OR to_regclass("
            "format('%I.hyperrag_knowledge_discovery_assets', current_schema())) "
            "IS NOT NULL AS present",
            [],
        )
        if existing and existing["present"]:
            await self.validate_search_schema()
            return
        for name in ("create_libraries_table", "create_assets_table", "create_trigram_extension"):
            await self.db.execute(SQL_TEMPLATES_KNOWLEDGE_DISCOVERY[name])
        for index_sql in SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["create_indexes"]:
            await self.db.execute(index_sql)

    async def validate_search_schema(self) -> None:
        try:
            incompatible = await self.db.query(
                """SELECT EXISTS (
                    SELECT 1 FROM hyperrag_knowledge_discovery_libraries
                    WHERE search_version IS DISTINCT FROM $1 OR search_tokens IS NULL
                    UNION ALL
                    SELECT 1 FROM hyperrag_knowledge_discovery_assets
                    WHERE search_version IS DISTINCT FROM $1 OR search_tokens IS NULL
                ) AS incompatible""",
                [discovery_token_version()],
            )
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError) as exc:
            raise RuntimeError("Discovery schema requires explicit catalog migration") from exc
        if incompatible["incompatible"]:
            raise RuntimeError("Discovery token version requires explicit catalog migration")
        indexes = await self.db.query(
            """SELECT count(*) AS ready FROM pg_index i
               WHERE i.indexrelid = ANY(ARRAY[
                 to_regclass('idx_hyperrag_discovery_libraries_tokens'),
                 to_regclass('idx_hyperrag_discovery_assets_tokens'),
                 to_regclass('idx_hyperrag_discovery_libraries_search'),
                 to_regclass('idx_hyperrag_discovery_assets_search')])
               AND i.indisvalid AND i.indisready
               AND i.indrelid IN (
                 to_regclass(format('%I.hyperrag_knowledge_discovery_libraries', current_schema())),
                 to_regclass(format('%I.hyperrag_knowledge_discovery_assets', current_schema()))
               )""",
            [],
        )
        if indexes["ready"] != 4:
            raise RuntimeError("Discovery indexes require explicit catalog migration")

    async def finalize(self) -> None:
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    @staticmethod
    def _to_database_datetime(value: float | None) -> datetime.datetime | None:
        if value is None:
            return None
        return datetime.datetime.fromtimestamp(float(value), tz=datetime.UTC).replace(tzinfo=None)

    @staticmethod
    def _to_timestamp(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.UTC)
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        parsed = datetime.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.UTC)
        return parsed.timestamp()

    @classmethod
    def _to_library(cls, row: dict[str, Any]) -> KnowledgeDiscoveryLibrary:
        return KnowledgeDiscoveryLibrary(
            library_id=str(row["library_id"]),
            title=str(row.get("title") or ""),
            overview=str(row.get("overview") or ""),
            asset_count=int(row.get("asset_count") or 0),
            document_count=int(row.get("document_count") or 0),
            video_count=int(row.get("video_count") or 0),
            normalized_search_text=str(row.get("normalized_search_text") or ""),
            updated_at=cls._to_timestamp(row.get("updated_at")) or time.time(),
            search_tokens=tuple(row.get("search_tokens") or []),
            search_version=str(row.get("search_version") or ""),
        )

    @classmethod
    def _to_asset(cls, row: dict[str, Any]) -> KnowledgeDiscoveryAsset:
        return KnowledgeDiscoveryAsset(
            library_id=str(row["library_id"]),
            asset_id=str(row["asset_id"]),
            title=str(row.get("title") or ""),
            asset_type=str(row.get("asset_type") or ""),
            summary=str(row.get("summary") or ""),
            normalized_search_text=str(row.get("normalized_search_text") or ""),
            is_active=bool(row.get("is_active")),
            status_updated_at=cls._to_timestamp(row.get("status_updated_at")),
            updated_at=cls._to_timestamp(row.get("updated_at")) or time.time(),
            search_tokens=tuple(row.get("search_tokens") or []),
            search_version=str(row.get("search_version") or ""),
        )

    async def upsert_library(self, library: KnowledgeDiscoveryLibrary) -> KnowledgeDiscoveryLibrary:
        await self._ensure_initialized()
        await self.db.execute(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["upsert_library"],
            [
                library.library_id,
                library.title,
                library.overview,
                library.asset_count,
                library.document_count,
                library.video_count,
                library.normalized_search_text,
                self._to_database_datetime(library.updated_at),
                list(library.search_tokens),
                library.search_version,
            ],
        )
        return library

    async def get_library(self, library_id: str) -> KnowledgeDiscoveryLibrary | None:
        await self._ensure_initialized()
        row = await self.db.query(SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["get_library"], [library_id])
        return self._to_library(row) if row else None

    async def list_libraries(self) -> list[KnowledgeDiscoveryLibrary]:
        await self._ensure_initialized()
        rows = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["list_libraries"], [], multirows=True
        )
        return [self._to_library(row) for row in rows or []]

    async def upsert_asset(self, asset: KnowledgeDiscoveryAsset) -> KnowledgeDiscoveryAsset:
        await self._ensure_initialized()
        await self.db.execute(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["upsert_asset"],
            [
                asset.library_id,
                asset.asset_id,
                asset.title,
                asset.asset_type,
                asset.summary,
                asset.normalized_search_text,
                asset.is_active,
                self._to_database_datetime(asset.status_updated_at),
                self._to_database_datetime(asset.updated_at),
                list(asset.search_tokens),
                asset.search_version,
            ],
        )
        return asset

    async def get_asset(self, library_id: str, asset_id: str) -> KnowledgeDiscoveryAsset | None:
        await self._ensure_initialized()
        row = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["get_asset"], [library_id, asset_id]
        )
        return self._to_asset(row) if row else None

    async def list_assets(self, library_id: str) -> list[KnowledgeDiscoveryAsset]:
        await self._ensure_initialized()
        rows = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["list_assets"], [library_id], multirows=True
        )
        return [self._to_asset(row) for row in rows or []]

    async def synchronize_library(
        self,
        library: KnowledgeDiscoveryLibrary,
        assets: list[KnowledgeDiscoveryAsset],
    ) -> None:
        """Persist a complete library snapshot and mark omitted assets inactive."""
        active_asset_ids: list[str] = []
        seen_asset_ids: set[str] = set()
        for asset in assets:
            if asset.library_id != library.library_id:
                raise ValueError("Discovery asset library_id must match its library snapshot")
            if asset.asset_id in seen_asset_ids:
                raise ValueError("Discovery asset IDs must be unique within a library snapshot")
            seen_asset_ids.add(asset.asset_id)
            active_asset_ids.append(asset.asset_id)
        async with self.library_transaction(library.library_id) as storage:
            current = await storage.get_library(library.library_id)
            current_assets = await storage.list_assets(library.library_id)
            if current is None or catalog_content(current) != catalog_content(library):
                await storage.upsert_library(library)
            by_id = {asset.asset_id: asset for asset in current_assets}
            for asset in assets:
                previous = by_id.get(asset.asset_id)
                if previous is None or catalog_content(previous) != catalog_content(asset):
                    await storage.upsert_asset(asset)
            if any(
                asset.is_active and asset.asset_id not in seen_asset_ids for asset in current_assets
            ):
                await storage.db.execute(
                    SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["deactivate_missing_assets"],
                    [
                        library.library_id,
                        active_asset_ids,
                        self._to_database_datetime(library.updated_at),
                    ],
                )

    @asynccontextmanager
    async def library_transaction(
        self,
        library_id: str,
    ) -> AsyncIterator[KnowledgeDiscoveryCatalogStorage]:
        await self._ensure_initialized()
        async with self.db.transaction() as db:
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                [f"hyperrag:discovery:{library_id}"],
            )
            storage = replace(self, db=db)
            storage._initialized = True
            yield storage

    async def list_all_assets(self) -> list[KnowledgeDiscoveryAsset]:
        await self._ensure_initialized()
        rows = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["list_all_assets"],
            [],
            multirows=True,
        )
        return [self._to_asset(row) for row in rows]

    async def delete_library(self, library_id: str) -> None:
        await self._ensure_initialized()
        await self.db.execute(SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["delete_library"], [library_id])

    async def delete_asset(self, library_id: str, asset_id: str) -> None:
        await self._ensure_initialized()
        await self.db.execute(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["delete_asset"], [library_id, asset_id]
        )

    async def search_libraries(
        self,
        query: str,
        *,
        limit: int | None = 20,
        library_ids: list[str] | None = None,
    ) -> list[KnowledgeDiscoveryLibrary]:
        await self._ensure_initialized()
        if not query.strip():
            libraries = await self.list_libraries()
            if library_ids is not None:
                libraries = [item for item in libraries if item.library_id in library_ids]
            return libraries if limit is None else libraries[: _optional_limit(limit)]
        normalized_query = normalize_search_text(query)
        tokens, patterns = query_terms(query)
        rows = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["search_libraries"],
            [normalized_query, tokens, patterns, _optional_limit(limit), library_ids],
            multirows=True,
        )
        return self._sort_libraries([self._to_library(row) for row in rows or []], normalized_query)

    async def search_assets(
        self,
        query: str,
        *,
        library_id: str | None = None,
        limit: int = 20,
    ) -> list[KnowledgeDiscoveryAsset]:
        await self._ensure_initialized()
        tokens, patterns = query_terms(query)
        normalized_query = normalize_search_text(query)
        rows = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["search_assets"],
            [
                normalized_query,
                tokens,
                patterns,
                max(1, int(limit)),
                library_id,
            ],
            multirows=True,
        )
        return self._sort_assets([self._to_asset(row) for row in rows or []], normalized_query)

    async def search_asset_library_ids(
        self,
        query: str,
        *,
        limit: int | None = 20,
        library_ids: list[str] | None = None,
    ) -> list[str]:
        """Return distinct matching library IDs without allowing one library to crowd out others."""
        await self._ensure_initialized()
        tokens, patterns = query_terms(query)
        normalized_query = normalize_search_text(query)
        rows = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["search_asset_library_ids"],
            [normalized_query, tokens, patterns, _optional_limit(limit), library_ids],
            multirows=True,
        )
        return [str(row["library_id"]) for row in rows or [] if str(row.get("library_id") or "")]

    @staticmethod
    def _match_rank(title: str, text: str, query: str) -> int:
        if not query:
            return 0
        normalized_title = normalize_search_text(title)
        if normalized_title == query:
            return 0
        if normalized_title.startswith(query):
            return 1
        if query in normalized_title:
            return 2
        if query in text:
            return 3
        matched_terms = sum(term in text for term in discovery_tokens(query))
        if matched_terms:
            return 4 - min(matched_terms, 3)
        return 4

    @classmethod
    def _sort_libraries(
        cls, libraries: list[KnowledgeDiscoveryLibrary], query: str
    ) -> list[KnowledgeDiscoveryLibrary]:
        return sorted(
            libraries,
            key=lambda item: (
                cls._match_rank(item.title, item.normalized_search_text, query),
                -item.updated_at,
                item.library_id,
            ),
        )

    @classmethod
    def _sort_assets(
        cls, assets: list[KnowledgeDiscoveryAsset], query: str
    ) -> list[KnowledgeDiscoveryAsset]:
        return sorted(
            assets,
            key=lambda item: (
                cls._match_rank(item.title, item.normalized_search_text, query),
                -item.updated_at,
                item.asset_id,
            ),
        )


_knowledge_discovery_catalog_storage: KnowledgeDiscoveryCatalogStorage | None = None


def catalog_content(item: KnowledgeDiscoveryLibrary | KnowledgeDiscoveryAsset) -> dict[str, Any]:
    """Compare materialized content, not the clock of an unrelated source update."""
    result = asdict(item)
    del result["updated_at"]
    if "status_updated_at" in result:
        result["status_updated_at"] = KnowledgeDiscoveryCatalogStorage._to_database_datetime(
            result["status_updated_at"],
        )
    return result


def _optional_limit(limit: int | None) -> int:
    return 0 if limit is None else max(1, int(limit))


def get_knowledge_discovery_catalog_storage() -> KnowledgeDiscoveryCatalogStorage:
    global _knowledge_discovery_catalog_storage
    if _knowledge_discovery_catalog_storage is None:
        _knowledge_discovery_catalog_storage = KnowledgeDiscoveryCatalogStorage(
            namespace="knowledge_discovery_catalog",
            workspace="",
            global_config={},
            embedding_func=None,
        )
    return _knowledge_discovery_catalog_storage


def library_to_dict(library: KnowledgeDiscoveryLibrary) -> dict[str, Any]:
    return {
        "library_id": library.library_id,
        "title": library.title,
        "overview": library.overview,
        "asset_count": library.asset_count,
        "document_count": library.document_count,
        "video_count": library.video_count,
        "normalized_search_text": library.normalized_search_text,
        "updated_at": library.updated_at,
    }


def asset_to_dict(asset: KnowledgeDiscoveryAsset) -> dict[str, Any]:
    return {
        "library_id": asset.library_id,
        "asset_id": asset.asset_id,
        "title": asset.title,
        "type": asset.asset_type,
        "summary": asset.summary,
        "normalized_search_text": asset.normalized_search_text,
        "is_active": asset.is_active,
        "status_updated_at": asset.status_updated_at,
        "updated_at": asset.updated_at,
    }
