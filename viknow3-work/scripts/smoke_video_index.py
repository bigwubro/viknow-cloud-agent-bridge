#!/usr/bin/env python3
"""API smoke: storage upload → index-jobs on ViKnow API → poll (default 5 min)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[int, Any]:
    req = request.Request(url, data=body, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            status = resp.status
    except error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except error.URLError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode())
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def _auth_headers(app_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {app_key}", "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def _smallest_repo_mp4(repo: Path) -> Path:
    candidates = sorted(repo.rglob("*.mp4"), key=lambda p: p.stat().st_size)
    if not candidates:
        raise FileNotFoundError(f"no .mp4 under {repo}")
    return candidates[0]


def prepare_tiny_video(dest: Path, repo_root: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:d=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(dest),
        ]
        subprocess.run(cmd, check=True)
        return dest
    fallback = _smallest_repo_mp4(repo_root)
    shutil.copy2(fallback, dest)
    return dest


def poll_job(
    base: str,
    app_key: str,
    job_id: str,
    timeout_s: float,
    interval_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, job = _http(
            "GET",
            f"{base}/api/v1/knowledge/index-jobs/{job_id}",
            headers=_auth_headers(app_key),
        )
        if status != 200 or not isinstance(job, dict):
            print(f"poll job HTTP {status}: {job}", flush=True)
        else:
            last = job
            st = str(job.get("status") or "")
            print(f"job {job_id}: {st}", flush=True)
            _, tasks = _http(
                "GET",
                f"{base}/api/v1/knowledge/index-jobs/{job_id}/tasks",
                headers=_auth_headers(app_key),
            )
            if isinstance(tasks, list) and tasks:
                tail = tasks[-1]
                print(
                    f"  task: {tail.get('name') or tail.get('task')}: {tail.get('status')}",
                    flush=True,
                )
            if st in {"done", "failed"}:
                return job
        time.sleep(interval_s)
    raise TimeoutError(f"job {job_id} not finished in {timeout_s}s (last={last})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("VIKNOW_SMOKE_BASE_URL", "http://127.0.0.1:5175"),
    )
    parser.add_argument(
        "--storage-url",
        default=os.environ.get("VIKNOW_STORAGE_URL", ""),
    )
    parser.add_argument(
        "--storage-token",
        default=os.environ.get("VIKNOW_STORAGE_TOKEN", ""),
    )
    parser.add_argument("--app-key", default=os.environ.get("VIKNOW3_APP_KEY", ""))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--compile-mode",
        default="fast_ingest",
        choices=("fast_ingest", "pre_compile"),
    )
    args = parser.parse_args()

    app_key = args.app_key.strip()
    if not app_key:
        demo = Path.home() / "viknow3" / "demo.env"
        if demo.is_file():
            for line in demo.read_text().splitlines():
                if line.startswith("VIKNOW3_APP_KEY="):
                    app_key = line.split("=", 1)[1].strip()
                    break
    if not app_key:
        print("VIKNOW3_APP_KEY is required", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    storage_url = args.storage_url.strip().rstrip("/")
    storage_token = args.storage_token.strip()
    if not storage_url or not storage_token:
        print("VIKNOW_STORAGE_URL and VIKNOW_STORAGE_TOKEN are required", file=sys.stderr)
        return 2

    library_id = f"smoke-vid-{uuid.uuid4().hex[:10]}"
    video_path = Path("/tmp") / f"viknow-smoke-{uuid.uuid4().hex[:8]}.mp4"
    video_path = prepare_tiny_video(video_path, args.repo_root.resolve())
    content = video_path.read_bytes()
    print(f"video={video_path} bytes={len(content)} library={library_id}", flush=True)

    asset_id = f"asset-{uuid.uuid4().hex[:10]}"
    with httpx.Client(
        base_url=storage_url,
        headers={"Authorization": f"Bearer {storage_token}"},
        timeout=120.0,
    ) as storage:
        lib_resp = storage.post(
            "/libraries",
            json={"library_id": library_id, "title": f"Smoke video {library_id}"},
        )
        if lib_resp.status_code not in (201, 409):
            print(f"storage create library failed {lib_resp.status_code}: {lib_resp.text}", file=sys.stderr)
            return 1
        up = storage.post(
            f"/libraries/{library_id}/assets",
            data={"asset_id": asset_id, "title": "smoke-tiny.mp4"},
            files={"file": ("smoke-tiny.mp4", content, "video/mp4")},
        )
        if up.status_code not in (201, 409):
            print(f"storage upload failed {up.status_code}: {up.text}", file=sys.stderr)
            return 1
    print(f"storage asset_id={asset_id}", flush=True)

    job_body = json.dumps(
        {
            "library_id": library_id,
            "asset_id": asset_id,
            "compile_mode": args.compile_mode,
            "params": {"title": "smoke-tiny.mp4"},
        },
    ).encode()
    status, job = _http(
        "POST",
        f"{base}/api/v1/knowledge/index-jobs",
        headers=_auth_headers(app_key, {"Content-Type": "application/json"}),
        body=job_body,
    )
    if status != 200 or not isinstance(job, dict):
        print(f"index job submit failed {status}: {job}", file=sys.stderr)
        return 1
    job_id = str(job.get("job_id") or "")
    print(f"submitted job_id={job_id}", flush=True)
    if not job_id:
        return 1

    try:
        finished = poll_job(base, app_key, job_id, args.timeout, args.poll_interval)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("result:", json.dumps(finished, ensure_ascii=False), flush=True)
    if str(finished.get("status")) != "done":
        return 1
    print("SMOKE_PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
