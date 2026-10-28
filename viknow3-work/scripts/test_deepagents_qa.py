#!/usr/bin/env python3
"""Single-agent deepagents KB QA smoke on playground."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def req_json(base: str, auth: str, method: str, path: str, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", "Authorization": auth}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=300) as resp:
        payload = resp.read()
        return json.loads(payload.decode()) if payload else None


def parse_sse(raw: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def fetch_history(base: str, auth: str, session_id: str, run_id: str) -> list[dict[str, Any]]:
    try:
        return req_json(
            base, auth, "GET", f"/api/v1/agent/agents/expertbuddy-deepagents/sessions/{session_id}/runs/{run_id}/history"
        )
    except urllib.error.HTTPError:
        request = urllib.request.Request(
            f"{base}/api/v1/agent/agents/expertbuddy-deepagents/sessions/{session_id}/runs/{run_id}/events?after_sequence=0&live=false",
            headers={"Authorization": auth},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            return parse_sse(resp.read())


def run_qa(base: str, auth: str, user_id: str, library_id: str, qid: str, message: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    out = req_json(
        base,
        auth,
        "POST",
        "/api/v1/agent/agents/expertbuddy-deepagents/sessions",
        {"user_id": user_id, "message": message, "library_ids": [library_id]},
    )
    sid, rid = out["session_id"], out["run_id"]
    run: dict[str, Any] = {"status": "running"}
    for _ in range(180):
        run = req_json(base, auth, "GET", f"/api/v1/agent/agents/expertbuddy-deepagents/sessions/{sid}/runs/{rid}")
        if run.get("status") in ("completed", "failed", "interrupted"):
            break
        time.sleep(2)
    latency = round(time.perf_counter() - t0, 2)
    events = fetch_history(base, auth, sid, rid)
    blob = json.dumps(events, ensure_ascii=False)
    tools = [
        str((ev.get("payload") or {}).get("name"))
        for ev in events
        if ev.get("type") == "tool_call_started" and (ev.get("payload") or {}).get("name")
    ]
    output = run.get("output") or ""
    retrieval = any("knowledge" in n.lower() for n in tools) or bool(
        re.search(r"knowledge_search|hyperrag|snippet", blob, re.I)
    )
    return {
        "question_id": qid,
        "status": run.get("status"),
        "latency_s": latency,
        "tools": tools,
        "retrieval_hit": retrieval,
        "citations": run.get("citations") or [],
        "output": output,
        "error": run.get("error") or run.get("error_code"),
        "session_id": sid,
        "run_id": rid,
    }


def main() -> int:
    base = os.environ.get("PLAYGROUND_BASE", "http://127.0.0.1:5198")
    library = os.environ.get("KB_QA_LIBRARY_ID", "e2e-artifacts-20261022")
    app_key = os.environ.get("VIKNOW3_APP_KEY", "dev-app-key")
    auth = f"Bearer {app_key}"
    user_id = req_json(base, auth, "POST", "/api/v1/users", {"display_name": "deepagents-qa-test"})["id"]
    questions = [
        ("doc", "ViKnow索引验收20261022文档写了什么？请基于知识库回答并给出引用。"),
        ("vague", "文档验收短语是什么？"),
    ]
    results = []
    for qid, text in questions:
        try:
            results.append(run_qa(base, auth, user_id, library, qid, text))
        except Exception as exc:  # noqa: BLE001
            results.append({"question_id": qid, "status": "error", "error": str(exc)})
    out_path = os.environ.get("DEEPAGENTS_QA_LOG", "/tmp/deepagents-qa-results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"library": library, "results": results}, f, ensure_ascii=False, indent=2)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if r.get("status") != "completed"]
    ok_retrieval = [r for r in results if r.get("retrieval_hit")]
    if failed:
        return 1
    if len(ok_retrieval) < 1:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
