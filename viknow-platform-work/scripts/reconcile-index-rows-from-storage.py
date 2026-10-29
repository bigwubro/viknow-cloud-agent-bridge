#!/usr/bin/env python3
"""Ensure platform index shadow rows exist for every asset in viknow-storage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from viknow_platform.access.knowledge import store
from viknow_platform.service.storage.gateway import get_storage_client, storage_configured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-id", default="", help="Only reconcile one library")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not storage_configured():
        print("viknow-storage not configured", file=sys.stderr)
        return 2

    client = get_storage_client()
    libraries = client.list_libraries(limit=100)
    if args.library_id.strip():
        libraries = [row for row in libraries if row.get("library_id") == args.library_id.strip()]

    created = 0
    for lib in libraries:
        library_id = str(lib.get("library_id") or "").strip()
        if not library_id:
            continue
        if args.dry_run:
            if store.get_library(library_id) is None:
                print(f"would ensure library shadow {library_id}")
        else:
            store.ensure_index_library_row(library_id, title=str(lib.get("title") or library_id))
        assets = client.list_assets(library_id, limit=100)
        for asset in assets:
            asset_id = str(asset.get("asset_id") or "").strip()
            if not asset_id:
                continue
            if store.get_asset(library_id, asset_id) is not None:
                continue
            if args.dry_run:
                print(f"would ensure index row {library_id}/{asset_id}")
            else:
                store.ensure_index_asset_row(
                    library_id,
                    asset_id,
                    title=str(asset.get("title") or asset_id),
                )
                print(f"ensured index row {library_id}/{asset_id}")
            created += 1
    print(f"done ({created} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
