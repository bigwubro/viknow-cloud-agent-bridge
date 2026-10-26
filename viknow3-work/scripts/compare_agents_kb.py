#!/usr/bin/env python3
"""Compare deepagents vs adk on the same KB QA prompts (236 playground)."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TurnResult:
    agent: str
    question_id: str
    question: str
    status: str
    latency_s: float
    tool_names: list[str] = field(default_factory=list)
    retrieval_hit: bool = False
    answer_markers: dict[str, bool] = field(default_factory=dict)
    citations: list[Any] = field(default_factory=list)
    output: str = ""
    error: str | None = None
    session_id: str = ""
    run_id: str = ""


def _parse_sse(raw: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _req_json(base: str, auth: str, method: str, path: str, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", "Authorization": auth}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=300) as resp:
        payload = resp.read()
        if not payload:
            return None
        return json.loads(payload.decode())


def _fetch_history(base: str, auth: str, agent: str, session_id: str, run_id: str) -> list[dict[str, Any]]:
    try:
        return _req_json(base, auth, "GET", f"/api/v1/agent/agents/{agent}/sessions/{session_id}/runs/{run_id}/history")
    except urllib.error.HTTPError:
        request = urllib.request.Request(
            f"{base}/api/v1/agent/agents/{agent}/sessions/{session_id}/runs/{run_id}/events?after_sequence=0&live=false",
            headers={"Authorization": auth},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            return _parse_sse(resp.read())


def _run_turn(
    base: str,
    auth: str,
    agent: str,
    user_id: str,
    library_id: str,
    question_id: str,
    message: str,
) -> TurnResult:
    t0 = time.perf_counter()
    out = _req_json(
        base,
        auth,
        "POST",
        f"/api/v1/agent/agents/{agent}/sessions",
        {"user_id": user_id, "message": message, "library_ids": [library_id]},
    )
    sid, rid = out["session_id"], out["run_id"]
    run: dict[str, Any] = {"status": "running"}
    for _ in range(240):
        run = _req_json(base, auth, "GET", f"/api/v1/agent/agents/{agent}/sessions/{sid}/runs/{rid}")
        if run.get("status") in ("completed", "failed", "interrupted"):
            break
        time.sleep(2)
    latency = time.perf_counter() - t0
    events = _fetch_history(base, auth, agent, sid, rid)
    blob = json.dumps(events, ensure_ascii=False)
    tool_names: list[str] = []
    for ev in events:
        if ev.get("type") == "tool_call_started":
            name = (ev.get("payload") or {}).get("name")
            if name:
                tool_names.append(str(name))
    output = run.get("output") or ""
    raw_citations = run.get("citations") or []
    if isinstance(raw_citations, dict):
        evidence = raw_citations.get("evidence")
        citations = list(evidence) if isinstance(evidence, list) else []
    else:
        citations = list(raw_citations)
    markers = {
        "答案": "答案" in output,
        "引用": "引用" in output or bool(citations),
        "证据": "证据" in output or "evidence" in blob.lower(),
        "ViKnow": "viknow" in output.lower(),
        "20261022": "20261022" in output,
    }
    retrieval = any("knowledge" in n.lower() for n in tool_names) or bool(
        re.search(r'"evidence"|"snippet"|hyperrag|knowledge_search', blob, re.I)
    )
    last = events[-1] if events else {}
    return TurnResult(
        agent=agent,
        question_id=question_id,
        question=message,
        status=str(run.get("status") or "unknown"),
        latency_s=round(latency, 2),
        tool_names=tool_names,
        retrieval_hit=retrieval,
        answer_markers=markers,
        citations=citations,
        output=output,
        error=run.get("error") or run.get("error_code") or (last.get("payload") or {}).get("code"),
        session_id=sid,
        run_id=rid,
    )


def default_questions() -> list[tuple[str, str]]:
    return [
        ("doc", "ViKnow索引验收20261022文档写了什么"),
        ("video", "视频左上角有什么"),
        ("vague", "文档验收短语是什么"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("PLAYGROUND_BASE", "http://36.103.198.236:5198"))
    parser.add_argument("--library", default=os.environ.get("KB_QA_LIBRARY_ID", "e2e-artifacts-20261022"))
    parser.add_argument("--app-key", default=os.environ.get("VIKNOW3_APP_KEY", "dev-app-key"))
    parser.add_argument("--out-dir", default=os.environ.get("AGENT_COMPARE_LOG_DIR", "/opt/cursor/artifacts/agent-compare-logs"))
    parser.add_argument("--markdown", default="/opt/cursor/artifacts/agent-compare-deepagents-vs-adk.md")
    parser.add_argument("--multiturn", action="store_true")
    args = parser.parse_args()

    auth = f"Bearer {args.app_key}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    user_id = _req_json(args.base, auth, "POST", "/api/v1/users", {"display_name": "agent-compare"})["id"]

    results: list[TurnResult] = []
    for agent in ("deepagents", "adk"):
        for qid, text in default_questions():
            try:
                tr = _run_turn(args.base, auth, agent, user_id, args.library, qid, text)
            except Exception as exc:  # noqa: BLE001
                tr = TurnResult(agent, qid, text, "error", 0.0, error=str(exc))
            results.append(tr)
            (out_dir / f"{agent}-{qid}.json").write_text(
                json.dumps(tr.__dict__, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if args.multiturn:
            try:
                tr = _run_turn(
                    args.base,
                    auth,
                    agent,
                    user_id,
                    args.library,
                    "followup",
                    "刚才文档里提到的验收日期是哪一天？",
                )
            except Exception as exc:  # noqa: BLE001
                tr = TurnResult(agent, "followup", "", "error", 0.0, error=str(exc))
            results.append(tr)
            (out_dir / f"{agent}-followup.json").write_text(
                json.dumps(tr.__dict__, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    md_lines = [
        "# deepagents vs ADK 知识库问答对比",
        "",
        f"- 库：`{args.library}`",
        f"- API：`{args.base}`",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        "## 结果表",
        "",
        "| 智能体 | 问题 | 状态 | 耗时(s) | 工具调用 | 检索命中 | ViKnow | 20261022 | 引用数 |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- | ---: |",
    ]
    for tr in results:
        tools = ", ".join(tr.tool_names) if tr.tool_names else "—"
        md_lines.append(
            f"| {tr.agent} | {tr.question_id} | {tr.status} | {tr.latency_s} | {tools} | "
            f"{'是' if tr.retrieval_hit else '否'} | "
            f"{'是' if tr.answer_markers.get('ViKnow') else '否'} | "
            f"{'是' if tr.answer_markers.get('20261022') else '否'} | {len(tr.citations)} |"
        )
    md_lines.extend(
        [
            "",
            "两份 profile 各自维护。DeepAgents 用 `expert_buddy` 的检查技能；ADK 用 `adk_buddy` 的检索作答技能。",
            "对比的是同一知识库、同一组问题，不是同一份 YAML。",
            "",
            "## 叙述",
            "",
        ]
    )
    for agent in ("deepagents", "adk"):
        subset = [r for r in results if r.agent == agent]
        ok = sum(1 for r in subset if r.status == "completed")
        md_lines.append(
            f"**{agent}**：{ok}/{len(subset)} 轮完成；"
            f"平均耗时 {round(sum(r.latency_s for r in subset) / max(len(subset), 1), 2)}s。"
        )
        for tr in subset:
            preview = (tr.output or tr.error or "")[:280].replace("\n", " ")
            md_lines.append(f"- `{tr.question_id}`：{preview}")
        md_lines.append("")

    Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
    Path(args.markdown).write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps({"markdown": args.markdown, "results": [r.__dict__ for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"markdown": args.markdown, "log_dir": str(out_dir)}, ensure_ascii=False))
    failed = [r for r in results if r.status != "completed"]
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
