"""Tar packaging and unpacking for knowledge migration packages.

Package layout::

    manifest.json     # MigrationManifest
    files/{logical_path}  # original uploads + working-dir products
    pg/{table}.jsonl      # one row per line, keyed by logical/absolute paths
    graph/entities.jsonl  # Neo4j entity records
    graph/relations.jsonl # Neo4j relation records
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from viknow.core.knowledge.migration.models import MigrationManifest


class ManifestValidationError(ValueError):
    """Raised when a package manifest or payload is corrupt."""


def compute_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _add_file_member(tar: tarfile.TarFile, logical_path: str, source: Path) -> None:
    info = tarfile.TarInfo(name=f"files/{logical_path}")
    info.size = source.stat().st_size
    with source.open("rb") as file:
        tar.addfile(info, file)


def _json_default(value: Any) -> Any:
    """Encode non-primitive values (e.g. datetimes from PG/Neo4j) as JSON."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _add_jsonl_member(tar: tarfile.TarFile, name: str, rows: list[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n"
        for row in rows
    )
    info = tarfile.TarInfo(name=name)
    info.size = len(payload.encode("utf-8"))
    tar.addfile(info, io.BytesIO(payload.encode("utf-8")))


def build_package(
    *,
    output_path: Path,
    manifest: MigrationManifest,
    files_root: Path,
    pg_rows: dict[str, list[dict[str, Any]]],
    graph_rows: dict[str, list[dict[str, Any]]],
) -> Path:
    """Write a full package tarball from collected data.

    ``files`` entries reference files on disk by their logical path under
    ``files_root``; only entries listed in the manifest are included.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, mode="w:gz") as tar:
        manifest_payload = manifest.model_dump_json().encode("utf-8")
        manifest_info = tarfile.TarInfo(name="manifest.json")
        manifest_info.size = len(manifest_payload)
        tar.addfile(manifest_info, io.BytesIO(manifest_payload))

        for entry in manifest.files:
            source = files_root / entry.logical_path
            if not source.is_file():
                raise FileNotFoundError(
                    f"manifest file missing on disk: {source} (logical={entry.logical_path})"
                )
            _add_file_member(tar, entry.logical_path, source)

        for table_name, rows in pg_rows.items():
            _add_jsonl_member(tar, f"pg/{table_name}.jsonl", rows)

        for graph_name, rows in graph_rows.items():
            _add_jsonl_member(tar, f"graph/{graph_name}.jsonl", rows)

    return output_path


def extract_package(package_path: Path, dest_dir: Path) -> MigrationManifest:
    """Extract a package into ``dest_dir`` and validate integrity.

    The manifest is read first; every ``files/`` member is then verified against
    the manifest's sha256. Returns the validated manifest.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest: MigrationManifest | None = None
    expected_hashes: dict[str, str] = {}

    with tarfile.open(package_path, mode="r:gz") as tar:
        for member in tar:
            if member.name == "manifest.json":
                raw = tar.extractfile(member)
                if raw is None:
                    raise ManifestValidationError("manifest.json is empty")
                manifest = MigrationManifest.model_validate_json(raw.read())
                expected_hashes = {entry.logical_path: entry.sha256 for entry in manifest.files}
                continue
            if member.name.startswith("files/"):
                logical_path = member.name[len("files/") :]
                expected = expected_hashes.get(logical_path)
                if expected is None:
                    raise ManifestValidationError(
                        f"package file not declared in manifest: {logical_path}"
                    )
                raw = tar.extractfile(member)
                if raw is None:
                    raise ManifestValidationError(f"package file is empty: {logical_path}")
                digest = hashlib.sha256()
                while True:
                    block = raw.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                if digest.hexdigest() != expected:
                    raise ManifestValidationError(
                        f"sha256 mismatch for {logical_path}: expected {expected}, got "
                        f"{digest.hexdigest()}"
                    )
                tar.extract(member, dest_dir)
                continue
            tar.extract(member, dest_dir)

    if manifest is None:
        raise ManifestValidationError("manifest.json missing from package")
    return manifest


def iter_jsonl(package_path: Path, member_name: str) -> Iterator[dict[str, Any]]:
    """Yield rows from a ``pg/*.jsonl`` or ``graph/*.jsonl`` member without caching.

    Missing members are treated as empty tables so packages that omit zero-row
    JSONL files (e.g. no entities) still import cleanly.
    """
    with tarfile.open(package_path, mode="r:gz") as tar:
        try:
            member = tar.getmember(member_name)
        except KeyError:
            return
        raw = tar.extractfile(member)
        if raw is None:
            return
        for line in raw:
            line = line.strip()
            if line:
                yield json.loads(line)
