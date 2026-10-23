"""Knowledge migration service: async export/import orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viknow.core.config import (
    AppConfig,
    resolve_files_root,
    resolve_knowledge_exports_root,
)
from viknow.core.knowledge.migration.export import ExporterDeps, export_library
from viknow.core.knowledge.migration.importer import ImporterDeps, import_package
from viknow.core.knowledge.migration.jobs import MigrationJobManager
from viknow.core.knowledge.migration.models import (
    MigrationJob,
    ProgressCallback,
)
from viknow.core.knowledge.migration.storage import (
    MigrationStorageConfig,
    build_migration_storage_config,
)
from viknow.core.knowledge.uploads.store import KnowledgeUploadStore


@dataclass
class MigrationServiceDeps:
    files_root: Path
    storage_config: MigrationStorageConfig
    upload_store: KnowledgeUploadStore
    exports_root: Path


class KnowledgeMigrationService:
    """High-level entry point used by the API layer."""

    def __init__(self, deps: MigrationServiceDeps):
        self._deps = deps
        self._jobs = MigrationJobManager()

    @property
    def exports_root(self) -> Path:
        return self._deps.exports_root

    def _exporter_deps(self) -> ExporterDeps:
        return ExporterDeps(
            files_root=self._deps.files_root,
            storage_config=self._deps.storage_config,
            upload_store=self._deps.upload_store,
        )

    def _importer_deps(self) -> ImporterDeps:
        return ImporterDeps(
            files_root=self._deps.files_root,
            storage_config=self._deps.storage_config,
            upload_store=self._deps.upload_store,
        )

    def _export_target_path(self, library_id: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return self._deps.exports_root / library_id / f"{library_id}-{stamp}.tar.gz"

    async def submit_export(self, library_id: str) -> str:
        output_path = self._export_target_path(library_id)
        deps = self._exporter_deps()

        async def runner(progress: ProgressCallback | None) -> dict[str, Any]:
            manifest = await export_library(
                deps=deps,
                library_id=library_id,
                output_path=output_path,
                progress=progress,
            )
            return {
                "package_path": str(output_path),
                "filename": output_path.name,
                "workspace": manifest.source_workspace,
                "files": len(manifest.files),
                "table_counts": manifest.table_counts,
            }

        return await self._jobs.submit(kind="export", library_id=library_id, runner=runner)

    async def submit_import(self, library_id: str, package_path: Path) -> str:
        deps = self._importer_deps()
        package_path = Path(package_path)

        async def runner(progress: ProgressCallback | None) -> dict[str, Any]:
            return await import_package(
                deps=deps,
                package_path=package_path,
                target_library_id=library_id,
                progress=progress,
            )

        return await self._jobs.submit(kind="import", library_id=library_id, runner=runner)

    async def get_job(self, job_id: str) -> MigrationJob | None:
        return await self._jobs.get(job_id)

    async def list_jobs(self, limit: int = 50) -> list[MigrationJob]:
        return await self._jobs.list_recent(limit=limit)

    def list_exports(self, library_id: str) -> list[dict[str, Any]]:
        directory = self._deps.exports_root / library_id
        if not directory.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.tar.gz"), reverse=True):
            results.append(
                {
                    "library_id": library_id,
                    "filename": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "created_at": path.stat().st_mtime,
                }
            )
        return results

    def list_all_exports(self) -> list[dict[str, Any]]:
        """List export packages across all libraries, newest first."""
        root = self._deps.exports_root
        if not root.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            if directory.name.startswith("_"):
                continue
            for path in directory.glob("*.tar.gz"):
                results.append(
                    {
                        "library_id": directory.name,
                        "filename": path.name,
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "created_at": path.stat().st_mtime,
                    }
                )
        results.sort(key=lambda item: item["created_at"], reverse=True)
        return results

    def resolve_export_package(self, library_id: str, filename: str) -> Path:
        safe_name = Path(filename).name
        package_path = self._deps.exports_root / library_id / safe_name
        if not package_path.is_file():
            raise FileNotFoundError(f"export package not found: {library_id}/{safe_name}")
        return package_path


def build_migration_service(
    config: AppConfig,
    upload_store: KnowledgeUploadStore,
) -> KnowledgeMigrationService:
    files_root = resolve_files_root(config)
    storage_config = build_migration_storage_config(config)
    return KnowledgeMigrationService(
        MigrationServiceDeps(
            files_root=files_root,
            storage_config=storage_config,
            upload_store=upload_store,
            exports_root=resolve_knowledge_exports_root(files_root),
        )
    )
