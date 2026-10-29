#!/usr/bin/env python3
"""
Fix HyperRAG index data in-place under a given directory.

Features:
1) Add a JSON header line to each chunk content in text_chunks storage:
   {"doc_id": "xxx", "segment_ids": ["seg-...", ...]}
2) Add parse_mode ("video" | "document") into asset_status storage.
3) Generate index.json snapshots for MinerU cache (similar to video_parser).

Usage:
  python scripts/fix_hyperrag_index.py /path/to/hyperrag_storage

Notes:
- This script attempts to be conservative: it won't change token counts or
  vector DB contents; only modifies text_chunks content, HyperRAG asset_status,
  and writes index.json under mineru_cache.
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Ensure project src is importable (src layout)
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_SRC = _SCRIPT_DIR.parent / "src"
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _find_workspace_dirs(root_dir: Path) -> List[Path]:
    """Return workspace directories under root_dir.
    If root_dir itself looks like a workspace (contains kv files), return [root_dir].
    Otherwise, return immediate subdirectories that look like a workspace.
    """
    def looks_like_workspace(d: Path) -> bool:
        names = {p.name for p in d.glob("kv_store_*.json")}
        targets = {"kv_store_text_chunks.json", "kv_store_doc_status.json", "kv_store_asset_status.json"}
        return len(names & targets) > 0

    if looks_like_workspace(root_dir):
        return [root_dir]

    workspaces: List[Path] = []
    for p in root_dir.iterdir():
        if p.is_dir() and looks_like_workspace(p):
            workspaces.append(p)
    return workspaces


def _detect_doc_status_file(workspace_dir: Path) -> Optional[Path]:
    # Prefer the canonical name, fallback to any doc_status-like file
    candidate = workspace_dir / "kv_store_doc_status.json"
    if candidate.exists():
        return candidate
    for p in workspace_dir.glob("kv_store_*doc_status*.json"):
        return p
    return None


def _load_doc_status_map(workspace_dir: Path) -> Dict[str, Dict[str, Any]]:
    path = _detect_doc_status_file(workspace_dir)
    return _load_json(path) if path else {}


def _is_header_line(s: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    try:
        first_line = (s.splitlines()[0] if s else "").strip()
        if first_line.startswith("{") and first_line.endswith("}"):
            obj = json.loads(first_line)
            if isinstance(obj, dict) and "doc_id" in obj and ("segment_ids" in obj or "order" in obj):
                return True, obj
    except Exception:
        pass
    return False, None


def _build_header_line_v1(doc_id: str, segment_ids: List[str]) -> str:
    try:
        return json.dumps({"doc_id": doc_id or "", "segment_ids": list(segment_ids or [])}, ensure_ascii=False)
    except Exception:
        return "{}"


def _build_header_line_v2(doc_id: str, order: int) -> str:
    try:
        return json.dumps({"doc_id": doc_id or "", "order": int(order)}, ensure_ascii=False)
    except Exception:
        return "{}"


def fix_text_chunks_headers(workspace_dir: Path) -> Tuple[int, int]:
    """Add header line to each chunk content in kv_store_text_chunks.json.
    Returns (total, updated)
    """
    path = workspace_dir / "kv_store_text_chunks.json"
    data = _load_json(path)
    if not data:
        return (0, 0)

    total = 0
    updated = 0
    for chunk_id, entry in list(data.items()):
        if not isinstance(entry, dict):
            continue
        total += 1
        content = str(entry.get("content", "") or "")
        doc_id = str(entry.get("doc_id") or entry.get("full_doc_id") or "")
        # 新目标头基于 chunk_order_index
        order = entry.get("chunk_order_index")
        try:
            order = int(order)
        except Exception:
            order = None
        segment_ids = entry.get("segment_ids", [])
        if not isinstance(segment_ids, list):
            segment_ids = []

        has_header, header_obj = _is_header_line(content)
        if has_header:
            # Skip if already correct header
            try:
                if header_obj and header_obj.get("doc_id") == doc_id and "order" in header_obj and order is not None and int(header_obj.get("order")) == order:
                    continue
            except Exception:
                pass
            # Replace header to new format (v2). If it's old v1 with segment_ids, or mismatched, normalize to v2
            lines = content.splitlines()
            if order is None:
                # fallback: try to infer from 'order' field if present elsewhere
                order = entry.get("order")
                try:
                    order = int(order)
                except Exception:
                    order = 0
            new_first = _build_header_line_v2(doc_id, order)
            new_content = "\n".join([new_first] + lines[1:])
            data[chunk_id]["content"] = new_content
            updated += 1
        else:
            # No header: add v2 header if possible else use v1 fallback then will be migrated in next runs
            if order is None:
                order = entry.get("order")
                try:
                    order = int(order)
                except Exception:
                    order = 0
            header_line = _build_header_line_v2(doc_id, order)
            new_content = f"{header_line}\n{content}"
            data[chunk_id]["content"] = new_content
            updated += 1

    if updated:
        _write_json(path, data)
    return (total, updated)


_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm"}


def _infer_parse_mode_from_path(file_path: Optional[str]) -> str:
    try:
        if not file_path:
            return "document"
        ext = Path(file_path).suffix.lower()
        return "video" if ext in _VIDEO_EXTS else "document"
    except Exception:
        return "document"


def fix_asset_status_parse_mode(workspace_dir: Path, doc_status_map: Dict[str, Dict[str, Any]]) -> Tuple[int, int]:
    """Set parse_mode in kv_store_asset_status.json based on doc_status file_path.
    Returns (total, updated)
    """
    path = workspace_dir / "kv_store_asset_status.json"
    data = _load_json(path)
    if not data:
        return (0, 0)

    total = 0
    updated = 0
    for doc_id, rec in list(data.items()):
        if not isinstance(rec, dict):
            continue
        total += 1
        existing = rec.get("parse_mode")
        file_path = None
        try:
            ds = doc_status_map.get(doc_id) or {}
            file_path = ds.get("file_path")
        except Exception:
            pass
        parse_mode = _infer_parse_mode_from_path(file_path)
        if existing != parse_mode:
            rec["parse_mode"] = parse_mode
            data[doc_id] = rec
            updated += 1

    if updated:
        _write_json(path, data)
    return (total, updated)


def _find_mineru_method_dir(mineru_cache_root: Path, file_stem: str) -> Optional[Path]:
    base = mineru_cache_root / file_stem
    if not base.exists():
        return None
    # Try common methods in preference order
    for method in ("vlm", "auto", "ocr", "txt"):
        d = base / method
        if d.exists():
            content_list = list(d.glob("*_content_list.json"))
            middle = list(d.glob("*_middle.json"))
            if content_list and middle:
                return d
    # Fallback: any subdir containing the necessary files
    for d in base.iterdir():
        if d.is_dir():
            content_list = list(d.glob("*_content_list.json"))
            middle = list(d.glob("*_middle.json"))
            if content_list and middle:
                return d
    return None


def generate_mineru_index_json(workspace_dir: Path, doc_status_map: Dict[str, Dict[str, Any]]) -> Tuple[int, int]:
    """Create index.json under mineru_cache/<file_stem>/<method>/ if not exists.
    Returns (total_candidates, created)
    """
    try:
        from hyperrag.parser import MinerUParser  # type: ignore
    except Exception:
        MinerUParser = None  # type: ignore

    mineru_cache_root = workspace_dir / "mineru_cache"
    if not mineru_cache_root.exists():
        return (0, 0)

    total = 0
    created = 0
    for doc_id, ds in doc_status_map.items():
        file_path = ds.get("file_path")
        if not file_path:
            continue
        ext = Path(file_path).suffix.lower()
        if ext in _VIDEO_EXTS:
            continue  # only for documents
        file_stem = Path(file_path).stem
        method_dir = _find_mineru_method_dir(mineru_cache_root, file_stem)
        if not method_dir:
            continue
        total += 1
        index_file = method_dir / "index.json"
        if index_file.exists():
            continue
        if MinerUParser is None:
            continue
        parser = MinerUParser()
        # output_dir is mineru_cache_root; method_dir.name is the method
        asset, segments = parser._load_existing_results(mineru_cache_root, Path(file_path), doc_id)  # type: ignore[attr-defined]
        payload = {
            "doc_id": doc_id,
            "file_hash": asset.file_hash,
            "asset": asset.model_dump(),
            "segments": [s.model_dump() for s in segments],
        }
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        created += 1

    return (total, created)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fix HyperRAG index storages in-place")
    ap.add_argument("root_dir", type=str, help="HyperRAG working_dir or its parent containing workspaces")
    args = ap.parse_args()

    root = Path(args.root_dir).resolve()
    if not root.exists() or not root.is_dir():
        print(f"Given path is not a directory: {root}")
        sys.exit(1)

    workspaces = _find_workspace_dirs(root)
    if not workspaces:
        print(f"No workspace directories found under: {root}")
        sys.exit(0)

    for ws in workspaces:
        print(f"\nProcessing workspace: {ws}")
        # 1) Fix text_chunks headers
        total, updated = fix_text_chunks_headers(ws)
        print(f"  text_chunks: total={total}, updated={updated}")

        # Load doc_status for later steps
        doc_status_map = _load_doc_status_map(ws)

        # 2) Fix asset_status parse_mode
        total2, updated2 = fix_asset_status_parse_mode(ws, doc_status_map)
        print(f"  asset_status: total={total2}, updated={updated2}")

        # 3) Generate mineru index.json
        total3, created3 = generate_mineru_index_json(ws, doc_status_map)
        print(f"  mineru_cache index.json: candidates={total3}, created={created3}")


if __name__ == "__main__":
    main()


