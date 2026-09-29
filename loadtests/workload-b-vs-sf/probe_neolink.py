#!/usr/bin/env python3
"""Tiny Neolink discovery. Not the 30k B sweep. Key from NEOLINK_API_KEY."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("NL_BASE", "https://neolink.com/api/v1").rstrip("/")
KEY = os.environ.get("NEOLINK_API_KEY", "")


def get(path: str):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def main() -> None:
    if not KEY:
        raise SystemExit("NEOLINK_API_KEY empty")
    code, body = get("/models")
    ids = [x.get("id") for x in (body.get("data") or [])]
    qwen = [i for i in ids if i and "qwen3.6" in i.lower()]
    print(json.dumps({"models_http": code, "n": len(ids), "qwen3.6": qwen}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
