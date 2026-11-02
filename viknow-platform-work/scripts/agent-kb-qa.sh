#!/usr/bin/env bash
# QA both agents against an indexed knowledge library (236 playground).
set -euo pipefail

BASE="${PLAYGROUND_BASE:-http://127.0.0.1:5198}"
LIB="${KB_QA_LIBRARY_ID:-e2e-artifacts-20261022}"
DOC_PHRASE="${KB_QA_DOC_PHRASE:-ViKnow索引验收20261022文档}"
VID_PHRASE="${KB_QA_VIDEO_PHRASE:-ViKnow索引验收20261022视频}"
AUTH="Authorization: Bearer ${VIKNOW_APP_KEY:-dev-app-key}"

export KB_QA_BASE="$BASE" KB_QA_LIB="$LIB" KB_QA_DOC="$DOC_PHRASE" KB_QA_VID="$VID_PHRASE" KB_QA_AUTH="$AUTH"
python3 <<'PY'
import json, os, time, urllib.request

BASE = os.environ["KB_QA_BASE"]
LIB = os.environ["KB_QA_LIB"]
DOC = os.environ["KB_QA_DOC"]
VID = os.environ["KB_QA_VID"]
AUTH = os.environ["KB_QA_AUTH"]


def req(method, path, body=None):
    headers = {"Content-Type": "application/json", "Authorization": AUTH}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=300) as resp:
        return json.loads(resp.read().decode())


def run_agent(agent, uid, message):
    out = req(
        "POST",
        f"/api/v1/agent/agents/{agent}/sessions",
        {"user_id": uid, "message": message, "library_ids": [LIB]},
    )
    sid, rid = out["session_id"], out["run_id"]
    run = {"status": "running"}
    for _ in range(180):
        run = req("GET", f"/api/v1/agent/agents/{agent}/sessions/{sid}/runs/{rid}")
        if run.get("status") in ("completed", "failed", "interrupted"):
            break
        time.sleep(2)
    try:
        events = req("GET", f"/api/v1/agent/agents/{agent}/sessions/{sid}/runs/{rid}/history")
    except Exception:
        events = []
    return run, events


def summarize(agent, run, events, phrase):
    output = run.get("output") or ""
    blob = json.dumps(events, ensure_ascii=False)
    tools = [ev.get("tool_name") for ev in events if (ev.get("kind") or "").startswith("tool_call")]
    return {
        "agent": agent,
        "status": run.get("status"),
        "error": run.get("error") or run.get("error_code"),
        "tool_knowledge": any("knowledge" in str(t).lower() for t in tools if t) or "knowledge_search" in blob,
        "phrase_hit": phrase in output or phrase in blob,
        "output_preview": output[:400],
    }


uid = req("POST", "/api/v1/users", {"display_name": "kb-qa"})["id"]
results = [
    summarize("deepagents", *run_agent("deepagents", uid, f"请引用知识库原文：{DOC}"), DOC),
    summarize("deepagents", *run_agent("deepagents", uid, f"知识库视频验收「{VID}」相关内容？"), VID),
    summarize("adk", *run_agent("adk", uid, f"根据知识库说明：{DOC}"), DOC),
]
print(json.dumps({"library": LIB, "results": results}, ensure_ascii=False, indent=2))
ok = all(r["status"] == "completed" and (r["phrase_hit"] or r["tool_knowledge"]) for r in results)
raise SystemExit(0 if ok else 1)
PY
