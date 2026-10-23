"""Knowledge library migration: full export/import of a knowledge base.

See ``openspec/changes/knowledge-migration-import-export/design.md`` for the
design decisions (logical paths, id recomputation, embedding fingerprint gate,
idempotent merge).
"""

from viknow.core.knowledge.migration.models import (
    EmbeddingFingerprint,
    ManifestFileEntry,
    MigrationJob,
    MigrationJobStatus,
    MigrationManifest,
)
from viknow.core.knowledge.migration.service import (
    KnowledgeMigrationService,
    MigrationServiceDeps,
    build_migration_service,
)

__all__ = [
    "EmbeddingFingerprint",
    "ManifestFileEntry",
    "MigrationJob",
    "MigrationJobStatus",
    "MigrationManifest",
    "MigrationServiceDeps",
    "KnowledgeMigrationService",
    "build_migration_service",
]
