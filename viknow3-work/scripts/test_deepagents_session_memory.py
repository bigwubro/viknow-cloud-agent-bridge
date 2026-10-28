#!/usr/bin/env python3
"""Multi-turn memory: same session_id, second turn must recall first-turn secret."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

SECRET = os.environ.get("SESSION_MEMORY_SECRET", "青松")
TURN1 = os.environ.get(
    "SESSION_MEMORY_TURN1",
    f"请记住：本轮暗号是「{SECRET}」。只回复「已记住」四个字，不要解释。",
)
TURN2 = os.environ.get(
    "SESSION_MEMORY_TURN2",
    "刚才让你记住的暗号是什么？只回答暗号本身，不要其它内容。",
)


def req_json(base: str, auth: str, method: str, path: str, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", "Authorization": auth}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=300) as resp:
        payload = resp.read()
        return json.loads(payload.decode()) if payload else None


def wait_run(base: str, auth: str, session_id: str, run_id: str) -> dict[str, Any]:
    run: dict[str, Any] = {"status": "running"}
    for _ in range(180):
        run = req_json(
            base,
            auth,
            "GET",
            f"/api/v1/agent/agents/expertbuddy-deepagents/sessions/{session_id}/runs/{run_id}",
        )
        if run.get("status") in ("completed", "failed", "interrupted"):
            return run
        time.sleep(2)
    return run


def main() -> int:
    base = os.environ.get("PLAYGROUND_BASE", "http://127.0.0.1:5198")
    library = os.environ.get("KB_QA_LIBRARY_ID", "doc")
    app_key = os.environ.get("VIKNOW3_APP_KEY", "dev-app-key")
    auth = f"Bearer {app_key}"
    user_id = req_json(base, auth, "POST", "/api/v1/users", {"display_name": "session-memory-test"})["id"]

    first = req_json(
        base,
        auth,
        "POST",
        "/api/v1/agent/agents/expertbuddy-deepagents/sessions",
        {"user_id": user_id, "message": TURN1, "library_ids": [library]},
    )
    session_id = first["session_id"]
    run1 = wait_run(base, auth, session_id, first["run_id"])

    second = req_json(
        base,
        auth,
        "POST",
        f"/api/v1/agent/agents/expertbuddy-deepagents/sessions/{session_id}/runs",
        {"user_id": user_id, "message": TURN2},
    )
    run2 = wait_run(base, auth, session_id, second["run_id"])

    out2 = (run2.get("output") or "").strip()
    recalled = SECRET in out2
    result = {
        "session_id": session_id,
        "library_id": library,
        "secret": SECRET,
        "run1_status": run1.get("status"),
        "run2_status": run2.get("status"),
        "run2_output": out2,
        "recalled_secret": recalled,
    }
    out_path = os.environ.get("SESSION_MEMORY_LOG", "/tmp/deepagents-session-memory.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if run1.get("status") != "completed" or run2.get("status") != "completed":
        return 1
    if not recalled:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
