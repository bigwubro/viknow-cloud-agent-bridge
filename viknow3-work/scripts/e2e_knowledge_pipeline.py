#!/usr/bin/env python3
"""End-to-end knowledge pipeline: library → upload → index → search/segments."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.hyperrag_test_env import bootstrap_hyperrag_config


def _load_env_files() -> None:
    ops = Path.home() / ".viknow3" / "ops.env"
    if ops.is_file():
        for line in ops.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    pg = os.environ.get("VIKNOW3_PG_ENV", "").strip()
    if not pg:
        for candidate in (
            Path.home() / "viknow3" / "pg-test.env",
            Path.home() / "viknow3" / "pg-demo.env",
        ):
            if candidate.is_file():
                pg = str(candidate)
                break
    if pg and Path(pg).is_file():
        for line in Path(pg).read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    playground = Path("/data/viknow/knowledge/libraries/playground-cursor-agent")
    scratch = playground / "scratch"
    tmp = Path.home() / "viknow3" / "tmp"
    data_root = Path("/data/viknow")
    os.environ.setdefault("VIKNOW3_DATA_DIR", str(data_root))
    os.environ.setdefault("VIKNOW3_LIBRARIES_ROOT", str(playground))
    os.environ.setdefault("VIKNOW_SCRATCH_DIR", str(scratch))
    os.environ.setdefault("TMPDIR", str(tmp))
    os.environ.setdefault("VIKNOW_STATE_DIR", str(Path.home() / "viknow3" / "state"))
    job_logs = Path.home() / "viknow3" / "job-logs"
    os.environ.setdefault("HYPERRAG_JOB_LOG_DIR", str(job_logs))
    job_logs.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    playground.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    Path(os.environ["VIKNOW_STATE_DIR"]).mkdir(parents=True, exist_ok=True)


def _configure_embedding_env() -> None:
    from viknow3.ops.config.store import plain_provider

    emb = plain_provider("embedding") or {}
    model = str(emb.get("model_name") or "Qwen/Qwen3-VL-Embedding-2B")
    if model == "Qwen3-VL-Embedding-2B":
        model = "Qwen/Qwen3-VL-Embedding-2B"
    base = str(emb.get("base_url") or "http://ai.qingxiang.tech:8601/v1").rstrip("/")
    if "127.0.0.1" in base or "localhost" in base:
        base = "http://ai.qingxiang.tech:8601/v1"
    os.environ["VIKNOW_HYPERRAG_EMBEDDING_MODEL"] = os.environ.get("VIKNOW_HYPERRAG_EMBEDDING_MODEL", model)
    os.environ["VIKNOW_HYPERRAG_EMBEDDING_BASE_URL"] = os.environ.get("VIKNOW_HYPERRAG_EMBEDDING_BASE_URL", base)
    api_key = str(emb.get("api_key") or os.environ.get("VIKNOW_EMBEDDING_API_KEY") or "").strip()
    if api_key:
        os.environ.setdefault("VIKNOW_HYPERRAG_EMBEDDING_API_KEY", api_key)


def _configure_mineru_vlm_env() -> None:
    """MinerU vlm-http-client reads VLM endpoint credentials from the environment."""
    from viknow3.ops.config.model_resolve import resolve_openai_chat
    from viknow3.ops.config.runtime import load_runtime_bundle

    private = load_runtime_bundle().providers.get("private") or {}
    resolved = resolve_openai_chat("server-default")
    server = str(private.get("base_url") or resolved.base_url).rstrip("/")
    if "127.0.0.1" in server or "localhost" in server:
        server = os.environ.get(
            "VIKNOW_MINERU_VL_SERVER",
            "https://ai.qingxiang.tech:8443/v1",
        ).strip().rstrip("/")
    elif server.startswith("http://") and ":8443" in server:
        server = "https://" + server[len("http://") :]
    os.environ.setdefault("MINERU_VL_SERVER", server)
    api_key = str(private.get("api_key") or resolved.api_key or "").strip()
    if api_key:
        os.environ.setdefault("MINERU_VL_API_KEY", api_key)
        os.environ.setdefault("OPENAI_API_KEY", api_key)
    os.environ.setdefault("MINERU_VL_MODEL_NAME", "vllm/mineru")
    os.environ.setdefault("HYPERRAG_MINERU_VL_MODEL_NAME", "vllm/mineru")


def _ensure_mineru_on_path() -> None:
    """Route mineru through hyperrag-test with the host knowledge data dir bind-mounted."""
    import shutil

    if shutil.which("mineru") and not os.environ.get("VIKNOW_E2E_MINERU_WRAPPER"):
        return
    bin_dir = Path.home() / "viknow3" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    src = ROOT / "scripts" / "mineru-docker-wrapper"
    wrapper = bin_dir / "mineru"
    if wrapper.is_symlink() or wrapper.exists():
        wrapper.unlink(missing_ok=True)
    wrapper.symlink_to(src)
    os.environ["VIKNOW_E2E_MINERU_WRAPPER"] = "1"
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _ensure_library(library_id: str, title: str) -> None:
    from viknow3.access.knowledge import store as knowledge_store
    from viknow3.access.knowledge.service import KnowledgeError, create_library

    if knowledge_store.get_library(library_id) is not None:
        return
    try:
        create_library({"library_id": library_id, "title": title})
    except KnowledgeError as exc:
        if exc.status != 409:
            raise


def _ensure_asset(library_id: str, asset_id: str, asset_path: str, title: str, modality: str) -> None:
    from viknow3.access.knowledge import store as knowledge_store
    from viknow3.access.knowledge.service import KnowledgeError, create_asset

    if knowledge_store.get_asset(library_id, asset_id) is not None:
        return
    try:
        create_asset(library_id,
            {
                "asset_id": asset_id,
                "asset_path": asset_path,
                "title": title,
                "modality": modality,
            },
        )
    except KnowledgeError as exc:
        if exc.status != 409:
            raise


def _poll_index(job_id: str, *, timeout_s: float, interval_s: float) -> dict:
    from viknow3.access.knowledge.service import get_index_job

    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_index_job(job_id)
        status = str(last.get("status") or "")
        print(f"index job {job_id}: {status}", flush=True)
        if status in {"done", "failed"}:
            return last
        time.sleep(interval_s)
    raise TimeoutError(f"index job {job_id} did not finish within {timeout_s}s (last={last})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-id", default=os.environ.get("E2E_LIBRARY_ID", "e2e-hyperrag"))
    parser.add_argument("--asset-id", default="")
    parser.add_argument("--file", type=Path, required=True, help="Source file to upload and index")
    parser.add_argument("--query", default="安装", help="Search query after indexing")
    parser.add_argument("--app-key", default=os.environ.get("VIKNOW3_APP_KEY", "dev-app-key"))
    parser.add_argument("--compile-mode", choices=("pre_compile", "fast_ingest"), default="fast_ingest")
    parser.add_argument("--index-timeout", type=float, default=1800.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args()

    _load_env_files()
    _configure_embedding_env()
    _configure_mineru_vlm_env()
    bootstrap_hyperrag_config()
    _ensure_mineru_on_path()

    from viknow3.access.knowledge.service import create_index_job, upload_object
    from viknow3.application.hyperrag.client import HYPERAG
    from viknow3.application.hyperrag.object_gateway import install_object_store
    from viknow3.application.hyperrag.object_gateway import gateway_from_ops

    install_object_store(gateway_from_ops())

    from viknow3.application.hyperrag.runtime import init_hyperrag, start_background_runtime

    async def _boot_runtime() -> None:
        await init_hyperrag()
        await start_background_runtime()

    print("starting HyperRAG background runtime (scheduler workers)...", flush=True)
    asyncio.run(_boot_runtime())

    source = args.file.expanduser().resolve()
    if not source.is_file():
        print(f"missing file: {source}", file=sys.stderr)
        return 2

    library_id = args.library_id.strip()
    asset_id = args.asset_id.strip() or f"e2e-{uuid.uuid4().hex[:12]}"
    content = source.read_bytes()
    suffix = source.suffix.lower()
    modality = "video" if suffix in {".mp4", ".mov", ".mkv", ".webm"} else "document"

    print(f"library={library_id} asset={asset_id} file={source.name} bytes={len(content)}", flush=True)
    _ensure_library(library_id, f"E2E {library_id}")

    uploaded = upload_object(library_id, source.name, content, asset_id=asset_id)
    print("uploaded:", json.dumps(uploaded, ensure_ascii=False), flush=True)

    _ensure_asset(
        library_id,
        asset_id,
        uploaded["asset_path"],
        source.name,
        modality,
    )

    job = create_index_job(
        {
            "library_id": library_id,
            "asset_id": asset_id,
            "asset_path": uploaded["asset_path"],
            "compile_mode": args.compile_mode,
            "params": {"title": source.name},
        },
    )
    print("index submitted:", json.dumps(job, ensure_ascii=False), flush=True)

    finished = _poll_index(job["job_id"], timeout_s=args.index_timeout, interval_s=args.poll_interval)
    if finished.get("status") != "done":
        print("index failed:", json.dumps(finished, ensure_ascii=False), file=sys.stderr)
        return 1

    search = HYPERAG.search(library_id, args.query, None, top_k=5)
    evidence = search.get("evidence") or search.get("candidates") or []
    print("search hits:", len(evidence), flush=True)
    print(json.dumps(search, ensure_ascii=False, indent=2)[:8000], flush=True)
    if not evidence:
        return 1

    segments = HYPERAG.list_segments(library_id, asset_id, modality=modality, mode="map", limit=5)
    print("segments:", json.dumps(segments, ensure_ascii=False, indent=2)[:4000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
