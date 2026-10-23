"""Reuse immutable raw model responses only across identical course inputs."""

import hashlib
import json
import shutil

from .client import dumps, save

IDENTITY = (
    "workspace",
    "asset_id",
    "source_sha256",
    "sop_path",
    "file_title",
    "duration",
    "video_path",
    "background",
    "config",
    "outline_requested",
)


def seed_raw_cache(previous, destination, request):
    prior = json.loads((previous / "job.json").read_text())
    if prior.get("status") != "completed" or any(
        prior.get(key) != request.get(key) for key in IDENTITY
    ):
        return 0
    if prior.get("prompt_version") not in {
        "vicoach-chapters-v8",
        "vicoach-chapters-v9",
        "vicoach-chapters-v10",
    }:
        return 0
    source = previous / "source.json"
    if (
        not source.exists()
        or hashlib.sha256(dumps(json.loads(source.read_text())).encode()).hexdigest()
        != request["source_sha256"]
    ):
        return 0
    copied = 0
    for cache in (previous / "calls").glob("*"):
        if not cache.is_dir() or (cache / "invalid.response.json").exists():
            continue
        try:
            body = json.loads((cache / "request.json").read_text())
            response = json.loads((cache / "response.json").read_text())
            json.loads((cache / "stats.json").read_text())
            digest = hashlib.sha256(
                dumps({"endpoint": request["config"]["base_url"], "body": body}).encode()
            ).hexdigest()
            if digest != cache.name or response["choices"][0]["finish_reason"] != "stop":
                continue
            json.loads(response["choices"][0]["message"]["content"])
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        target = destination / "calls" / digest
        if target.exists():
            continue
        target.mkdir(parents=True)
        for name in ("request.json", "stats.json", "response.json"):
            shutil.copyfile(cache / name, target / name)
        copied += 1
    # Decode current media again: v8 did not persist a whole-video hash.
    # Exact model-request hashes also bind the freshly decoded image bytes.
    save(
        destination / "raw-cache-reuse.json",
        {
            "previous_job": previous.name,
            "calls_copied": copied,
            "identity_fields": list(IDENTITY),
            "final_outputs_reused": False,
        },
    )
    return copied
