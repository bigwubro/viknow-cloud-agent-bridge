"""Explicitly opt-in tests; all writes are confined to a newly created scratch schema."""

import asyncio
import os
import uuid
from dataclasses import replace

import asyncpg
import pytest
import pytest_asyncio
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.knowledge_discovery import (
    SQL_TEMPLATES_KNOWLEDGE_DISCOVERY as SQL,
)
from hyperrag.knowledge_discovery import (
    KnowledgeDiscoveryCatalogStorage,
)
from hyperrag.knowledgebase_info import (
    SQL_TEMPLATES_KNOWLEDGEBASE_INFO,
    KnowledgebaseInfo,
    KnowledgebaseInfoStorage,
)
from hyperrag.runtime_primitives import PostgreSQLDB


@pytest_asyncio.fixture
async def database(monkeypatch):
    dsn = os.environ.get("HYPERRAG_CATALOG_TEST_DSN")
    if not dsn:
        pytest.skip("Set HYPERRAG_CATALOG_TEST_DSN for isolated schema integration tests")
    schema = "catalog_validation_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn, command_timeout=30)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def initialize(connection):
        await connection.execute(f'SET search_path TO "{schema}", public')

    pool = None
    try:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=4,
            setup=initialize,
            command_timeout=30,
        )
        db = PostgreSQLDB(pool)
        for name in ("create_libraries_table", "create_assets_table"):
            await db.execute(SQL[name])
        for sql in SQL["create_indexes"]:
            await db.execute(sql)
        await db.execute(SQL_TEMPLATES_KNOWLEDGEBASE_INFO["create_table"])
        catalog = KnowledgeDiscoveryCatalogStorage(
            namespace="discovery-test",
            workspace="",
            global_config={},
            db=db,
        )
        await catalog.initialize()
        source = KnowledgebaseInfoStorage(
            namespace="source-test",
            workspace="",
            global_config={},
            db=db,
        )
        source._initialized = True
        monkeypatch.setattr("hyperrag.hyperrag_v2.get_knowledgebase_info_storage", lambda: source)
        runtime = object.__new__(HyperRAGV2)
        runtime._core_initialized = True
        runtime._knowledge_discovery_catalog_storage = catalog
        runtime._knowledge_discovery_backfill_ready = asyncio.Event()
        runtime._knowledge_discovery_backfill_ready.set()
        yield db, catalog, source, runtime
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def record():
    return KnowledgebaseInfo(
        id="fixture-kb",
        created_at=1,
        updated_at=10,
        name="刻蚀设备维护",
        asset_ids=["doc-1", "doc-2"],
        metadata={
            "overview": "真空工艺",
            "asset_summaries": {
                "doc-1": {
                    "title": "硅工艺 LAM-2300",
                    "summary": "Si+HBr 化学反应",
                    "type_label": "文档",
                    "status_updated_at": "10",
                },
                "doc-2": {"title": "维护手册", "summary": "氧化步骤", "type_label": "文档"},
            },
        },
    )


@pytest.mark.asyncio
async def test_noop_sql_and_initialization_do_not_physically_update(database):
    db, catalog, source, runtime = database
    library, assets = runtime._discovery_catalog_snapshot(record())
    await catalog.synchronize_library(library, assets)
    before = await db.query(
        "SELECT asset_id,xmin::text,updated_at FROM hyperrag_knowledge_discovery_assets "
        "ORDER BY asset_id",
        multirows=True,
    )
    async with catalog.library_transaction(library.library_id) as transaction:
        counters = "SELECT sum(n_tup_upd) AS updates FROM pg_stat_xact_user_tables"
        initial = await transaction.db.query(counters)
        await transaction.upsert_library(replace(library, updated_at=999))
        for asset in assets:
            await transaction.upsert_asset(replace(asset, updated_at=999))
        assert await transaction.db.query(counters) == initial
    assert (
        await db.query(
            "SELECT asset_id,xmin::text,updated_at FROM hyperrag_knowledge_discovery_assets "
            "ORDER BY asset_id",
            multirows=True,
        )
        == before
    )
    source_record = record()
    await source.upsert(
        source_record.id,
        name=source_record.name,
        asset_ids=source_record.asset_ids,
        metadata=source_record.metadata,
    )
    for _ in range(2):
        runtime._knowledge_discovery_backfill_ready.clear()
        await runtime._backfill_knowledge_discovery_catalog()
        await runtime._wait_for_knowledge_discovery_catalog()
    assert (
        await db.query(
            "SELECT asset_id,xmin::text,updated_at FROM hyperrag_knowledge_discovery_assets "
            "ORDER BY asset_id",
            multirows=True,
        )
        == before
    )


@pytest.mark.asyncio
async def test_source_and_catalog_roll_back_together(database, monkeypatch):
    db, catalog, source, runtime = database

    async def fail_asset(self, asset):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(KnowledgeDiscoveryCatalogStorage, "upsert_asset", fail_asset)
    with pytest.raises(RuntimeError, match="injected"):
        await runtime.aupsert_knowledgebase_info(
            "fixture-kb",
            name="guide",
            asset_ids=["new-doc"],
        )
    assert await source.get("fixture-kb") is None
    assert await catalog.get_library("fixture-kb") is None


@pytest.mark.asyncio
async def test_concurrent_stale_backfill_never_overwrites_new_source_or_revives_deletion(database):
    db, catalog, source, runtime = database
    old = await runtime.aupsert_knowledgebase_info("fixture-kb", name="old", asset_ids=[])
    await asyncio.gather(
        runtime._synchronize_knowledge_discovery_catalog(old),
        runtime.aupsert_knowledgebase_info("fixture-kb", name="new"),
    )
    assert (await source.get("fixture-kb")).name == "new"
    assert (await catalog.get_library("fixture-kb")).title == "new"
    await asyncio.gather(
        runtime._synchronize_knowledge_discovery_catalog(old),
        runtime.adelete_knowledgebase_info("fixture-kb"),
    )
    assert await source.get("fixture-kb") is None
    assert await catalog.get_library("fixture-kb") is None


@pytest.mark.asyncio
async def test_inactive_rows_are_not_rewritten_and_reactivation_works(database):
    db, catalog, source, runtime = database
    library, assets = runtime._discovery_catalog_snapshot(record())
    await catalog.synchronize_library(library, assets)
    await catalog.synchronize_library(replace(library, asset_count=1, updated_at=20), assets[:1])
    deleted = await db.query(
        "SELECT xmin::text,updated_at FROM hyperrag_knowledge_discovery_assets "
        "WHERE asset_id='doc-2'",
    )
    await catalog.synchronize_library(replace(library, asset_count=1, updated_at=30), assets[:1])
    assert (
        await db.query(
            "SELECT xmin::text,updated_at FROM hyperrag_knowledge_discovery_assets "
            "WHERE asset_id='doc-2'",
        )
        == deleted
    )
    await catalog.synchronize_library(replace(library, updated_at=40), assets)
    assert (await catalog.get_asset(library.library_id, "doc-2")).is_active


@pytest.mark.asyncio
async def test_real_sql_matches_chinese_chemistry_and_english_substrings(database):
    db, catalog, source, runtime = database
    library, assets = runtime._discovery_catalog_snapshot(record())
    await catalog.synchronize_library(library, assets)
    for query in ("硅", "Si+HBr=", "HBr", "LAM-2300", "2300", "刻蚀"):
        matches = await runtime.adiscover_library_matches(query)
        assert [item["library"]["library_id"] for item in matches["library_matches"]] == [
            library.library_id,
        ], query
    assert (await runtime.adiscover_library_matches("热干面的制作方法"))["library_matches"] == []


@pytest.mark.asyncio
async def test_missing_or_stale_schema_fails_before_becoming_ready(database):
    db, catalog, source, runtime = database
    library, assets = runtime._discovery_catalog_snapshot(record())
    await catalog.synchronize_library(library, assets)
    await db.execute(
        "UPDATE hyperrag_knowledge_discovery_libraries SET search_version='old-version'",
    )
    with pytest.raises(RuntimeError, match="migration"):
        await catalog.validate_search_schema()
    await db.execute(
        "UPDATE hyperrag_knowledge_discovery_libraries SET search_version=$1",
        [library.search_version],
    )
    await catalog.validate_search_schema()
    await db.execute("ALTER TABLE hyperrag_knowledge_discovery_assets DROP COLUMN search_tokens")
    with pytest.raises(RuntimeError, match="migration"):
        await catalog.validate_search_schema()


@pytest.mark.asyncio
async def test_offline_token_migration_and_compaction_preserve_catalog(database):
    from hyperrag.discovery_maintenance import (
        TABLES,
        assert_preserved,
        build_indexes,
        catalog_state,
        compact_tables,
        migrate_tokens,
        qualified_table,
        verify_tokens,
    )

    db, catalog, source, runtime = database
    library, assets = runtime._discovery_catalog_snapshot(record())
    await catalog.synchronize_library(library, assets)
    async with db.pool.acquire() as conn:
        schema = await conn.fetchval("SELECT current_schema()")
        for table in TABLES:
            await conn.execute(
                f"ALTER TABLE {qualified_table(schema, table)} "
                "DROP COLUMN search_tokens, DROP COLUMN search_version",
            )
        before = await catalog_state(conn, schema=schema)
        await migrate_tokens(conn, schema=schema)
        first = await conn.fetch(
            """SELECT asset_id,xmin::text
            FROM hyperrag_knowledge_discovery_assets ORDER BY asset_id""",
        )
        await migrate_tokens(conn, schema=schema)
        assert (
            await conn.fetch(
                """SELECT asset_id,xmin::text
                FROM hyperrag_knowledge_discovery_assets ORDER BY asset_id""",
            )
            == first
        )
        await compact_tables(conn, schema=schema)
        await build_indexes(conn, schema=schema)
        await verify_tokens(conn, schema=schema)
        after = await catalog_state(conn, schema=schema)
        assert_preserved(before, after)
        async with conn.transaction():
            with pytest.raises(RuntimeError, match="transaction"):
                await compact_tables(conn, schema=schema)
    assert await catalog.search_asset_library_ids("Si+HBr=") == [library.library_id]


@pytest.mark.asyncio
async def test_unrelated_source_metadata_does_not_rewrite_assets(database):
    db, catalog, source, runtime = database
    item = record()
    await runtime.aupsert_knowledgebase_info(
        item.id,
        name=item.name,
        asset_ids=item.asset_ids,
        metadata=item.metadata,
    )
    rows = "SELECT asset_id,xmin::text FROM hyperrag_knowledge_discovery_assets ORDER BY asset_id"
    before = await db.query(rows, multirows=True)
    await runtime.aupsert_knowledgebase_info(
        item.id,
        metadata={**item.metadata, "unrelated_metadata": "changed"},
    )
    assert await db.query(rows, multirows=True) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_stale_separate_runtime_rereads_after_contended_library_lock(
    database,
    monkeypatch,
    operation,
):
    db, catalog, source, runtime = database
    old = await runtime.aupsert_knowledgebase_info("fixture-kb", name="old")
    second = object.__new__(HyperRAGV2)
    second._knowledge_discovery_catalog_storage = catalog
    attempted = asyncio.Event()
    original = PostgreSQLDB.execute
    held_connection = None

    async def execute(self, sql, params=None, **kwargs):
        if params == ["hyperrag:discovery:fixture-kb"] and self.connection is not held_connection:
            attempted.set()
        return await original(self, sql, params, **kwargs)

    monkeypatch.setattr(PostgreSQLDB, "execute", execute)
    async with catalog.library_transaction(old.id) as transaction:
        held_connection = transaction.db.connection
        if operation == "update":
            latest = await source.with_db(transaction.db).upsert(old.id, name="new")
            await transaction.synchronize_library(*runtime._discovery_catalog_snapshot(latest))
        else:
            await source.with_db(transaction.db).delete(old.id)
            await transaction.delete_library(old.id)
        attempted.clear()
        pending = asyncio.create_task(second._synchronize_knowledge_discovery_catalog(old))
        await asyncio.wait_for(attempted.wait(), timeout=2)
        assert not pending.done()
    await asyncio.wait_for(pending, timeout=2)
    result = await catalog.get_library(old.id)
    if operation == "update":
        assert result.title == "new"
    else:
        assert result is None


@pytest.mark.asyncio
async def test_concurrent_first_initializers_observe_only_atomic_schema(database, monkeypatch):
    from contextlib import asynccontextmanager

    db, catalog, source, runtime = database
    await db.execute("DROP TABLE hyperrag_knowledge_discovery_assets")
    await db.execute("DROP TABLE hyperrag_knowledge_discovery_libraries")

    @asynccontextmanager
    async def separate_process_local_lock():
        yield

    monkeypatch.setattr(
        "hyperrag.knowledge_discovery.get_data_init_lock", separate_process_local_lock
    )
    created_library = asyncio.Event()
    release_creator = asyncio.Event()
    second_attempted = asyncio.Event()
    original = PostgreSQLDB.execute
    first_schema_lock = True

    async def execute(self, sql, params=None, **kwargs):
        nonlocal first_schema_lock
        if params == ["hyperrag:discovery:schema"]:
            if first_schema_lock:
                first_schema_lock = False
            else:
                second_attempted.set()
        result = await original(self, sql, params, **kwargs)
        if sql == SQL["create_libraries_table"]:
            created_library.set()
            await release_creator.wait()
        return result

    monkeypatch.setattr(PostgreSQLDB, "execute", execute)
    first, second = replace(catalog), replace(catalog)
    creator = asyncio.create_task(first.initialize())
    await asyncio.wait_for(created_library.wait(), timeout=2)
    follower = asyncio.create_task(second.initialize())
    await asyncio.wait_for(second_attempted.wait(), timeout=2)
    assert not follower.done()
    release_creator.set()
    await asyncio.wait_for(asyncio.gather(creator, follower), timeout=2)
    await first.validate_search_schema()
    await second.validate_search_schema()


@pytest.mark.asyncio
async def test_food_positive_is_retained_without_matching_semiconductor_catalog(database):
    db, catalog, source, runtime = database
    library, assets = runtime._discovery_catalog_snapshot(record())
    await catalog.synchronize_library(library, assets)
    food = KnowledgebaseInfo(
        id="fixture-food",
        name="热干面食谱",
        created_at=1,
        updated_at=2,
        asset_ids=["recipe"],
        metadata={
            "overview": "热干面的制作方法",
            "asset_summaries": {
                "recipe": {
                    "title": "热干面制作方法",
                    "type_label": "文档",
                    "summary": "芝麻酱拌面。",
                }
            },
        },
    )
    await catalog.synchronize_library(*runtime._discovery_catalog_snapshot(food))
    result = await runtime.adiscover_library_matches("热干面的制作方法")
    assert [item["library"]["library_id"] for item in result["library_matches"]] == [food.id]
