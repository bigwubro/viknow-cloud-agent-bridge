#!/usr/bin/env python3
"""Build embed-vl Grafana JSON: three legacy UIDs, one DP3 Prometheus instance (:8601)."""
from __future__ import annotations

import json
import re
from pathlib import Path

DS = {"type": "prometheus", "uid": "efphcpq654uf4f"}
INSTANCE = "127.0.0.1:8601"
MODEL = "Qwen/Qwen3-VL-Embedding-2B"

# Legacy bookmark UIDs → DP rank on GPUs 5,6,7 (engine 0/1/2). Latency/QPS panels stay instance-wide.
VIEWS = [
    {
        "uid": "vllm-embed-vl-8601",
        "title": f"vLLM {MODEL} · DP3 · vllm-embed-vl · :8601",
        "slug": "vllm-qwen3-vl-embedding-2b-dp3-8601",
        "legacy_port": "8601",
        "legacy_name": "vllm-embed-vl",
        "engine": None,
        "gpu": "5 / 6 / 7",
        "aggregate": True,
    },
    {
        "uid": "vllm-embed-vl-8611",
        "title": f"vLLM {MODEL} · DP3 · 原 :8611 · engine 0 · GPU5",
        "slug": "vllm-qwen3-vl-embedding-2b-8611-dp3",
        "legacy_port": "8611",
        "legacy_name": "vllm-embed-vl-g5",
        "engine": "0",
        "gpu": "5",
        "aggregate": False,
    },
    {
        "uid": "vllm-embed-vl-8612",
        "title": f"vLLM {MODEL} · DP3 · 原 :8612 · engine 2 · GPU7",
        "slug": "vllm-qwen3-vl-embedding-2b-8612-dp3",
        "legacy_port": "8612",
        "legacy_name": "vllm-embed-vl-g7-a",
        "engine": "2",
        "gpu": "7",
        "aggregate": False,
    },
]

ENGINE_IN_SELECTOR = re.compile(
    r'(\{instance=\\"\$instance\\")(\})',
)


def bump_panels(panels: list[dict], dy: int) -> None:
    for p in panels:
        if "gridPos" in p:
            p["gridPos"]["y"] = p["gridPos"].get("y", 0) + dy


def walk_panels(panels: list[dict]) -> list[dict]:
    out: list[dict] = []
    for p in panels:
        out.append(p)
        nested = p.get("panels")
        if nested:
            out.extend(walk_panels(nested))
    return out


def inject_engine_into_expr(expr: str, engine: str) -> str:
    if "engine=" in expr:
        return expr
    return ENGINE_IN_SELECTOR.sub(
        rf'\1,engine="{engine}"\2',
        expr,
    )


def patch_panel_engine(panel: dict, engine: str | None) -> None:
    if not engine:
        return
    for t in panel.get("targets") or []:
        expr = t.get("expr")
        if not expr or "$instance" not in expr:
            continue
        metric = expr.split("{", 1)[0] if "{" in expr else ""
        if metric.endswith(
            (
                "num_requests_running",
                "num_requests_waiting",
                "kv_cache_usage_perc",
            )
        ) or "num_requests_running" in expr or "num_requests_waiting" in expr:
            t["expr"] = inject_engine_into_expr(expr, engine)


def header_panels(spec: dict) -> tuple[list[dict], int]:
    eng = spec["engine"]
    if spec["aggregate"]:
        rank_line = "本页为 **三 rank 合计** + 下方 **engine 0/1/2** 分 rank 曲线。"
        row_title = f"DP3 · vllm-embed-vl · GPU {spec['gpu']} · :8601"
    else:
        rank_line = (
            f"原 **:{spec['legacy_port']}**（`{spec['legacy_name']}`）已并入 DP3；"
            f"Scheduler / KV 等按 **engine={eng}**（GPU **{spec['gpu']}**）过滤。"
            " E2E / QPS 仍为 **整实例**（Prometheus 无 per-engine 直方图）。"
        )
        row_title = f"DP3 · 原 :{spec['legacy_port']} · engine {eng} · :8601"

    panels: list[dict] = [
        {
            "id": 1,
            "type": "row",
            "title": row_title,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
            "collapsed": False,
            "panels": [],
        },
        {
            "id": 2,
            "type": "text",
            "title": "部署说明",
            "gridPos": {"h": 3, "w": 24, "x": 0, "y": 1},
            "options": {
                "mode": "markdown",
                "content": (
                    "三路单卡 **:8601 / :8611 / :8612** 已合并为 **一个** `vllm-embed-vl`："
                    "`--data-parallel-size 3`，对外 **:8601**，Prometheus 仅 scrape "
                    f"`{INSTANCE}`。\n\n{rank_line}"
                ),
            },
        },
    ]
    if spec["aggregate"]:
        panels.append(
            {
                "id": 3,
                "type": "timeseries",
                "title": "DP rank · running / waiting",
                "description": "同一 :8601 metrics 端点上的 engine 0/1/2。",
                "datasource": DS,
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": 4},
                "fieldConfig": {
                    "defaults": {
                        "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 15},
                        "min": 0,
                    },
                    "overrides": [],
                },
                "options": {
                    "legend": {"displayMode": "table", "placement": "bottom"},
                    "tooltip": {"mode": "multi"},
                },
                "targets": [
                    {
                        "expr": (
                            f'sum by (engine) (vllm:num_requests_running{{instance="{INSTANCE}"}}) '
                            "or on() vector(0)"
                        ),
                        "legendFormat": "running engine {{engine}}",
                        "refId": "A",
                        "datasource": DS,
                    },
                    {
                        "expr": (
                            f'sum by (engine) (vllm:num_requests_waiting{{instance="{INSTANCE}"}}) '
                            "or on() vector(0)"
                        ),
                        "legendFormat": "waiting engine {{engine}}",
                        "refId": "B",
                        "datasource": DS,
                    },
                ],
            }
        )
        header_h = 12
    else:
        panels.append(
            {
                "id": 3,
                "type": "timeseries",
                "title": f"engine {eng} · running / waiting",
                "datasource": DS,
                "gridPos": {"h": 6, "w": 24, "x": 0, "y": 4},
                "fieldConfig": {
                    "defaults": {
                        "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 20},
                        "min": 0,
                    },
                    "overrides": [],
                },
                "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
                "targets": [
                    {
                        "expr": (
                            f'sum(vllm:num_requests_running{{instance="{INSTANCE}",engine="{eng}"}}) '
                            "or on() vector(0)"
                        ),
                        "legendFormat": "running",
                        "refId": "A",
                        "datasource": DS,
                    },
                    {
                        "expr": (
                            f'sum(vllm:num_requests_waiting{{instance="{INSTANCE}",engine="{eng}"}}) '
                            "or on() vector(0)"
                        ),
                        "legendFormat": "waiting",
                        "refId": "B",
                        "datasource": DS,
                    },
                ],
            }
        )
        header_h = 10

    return panels, header_h


def build_dashboard(base_path: Path, spec: dict) -> dict:
    d = json.loads(base_path.read_text(encoding="utf-8"))
    header, dy = header_panels(spec)
    bump_panels(d["panels"], dy)
    d["panels"] = header + d["panels"]

    for panel in walk_panels(d["panels"]):
        patch_panel_engine(panel, spec["engine"])

    d["title"] = spec["title"]
    d["uid"] = spec["uid"]
    d["tags"] = ["vllm", "embed-vl", "qwen3-vl", "dp3", "236"]
    eng_note = "三 rank 合计" if spec["aggregate"] else f"engine {spec['engine']}（原 :{spec['legacy_port']}）"
    d["description"] = (
        f"{MODEL} 单进程 DP3，Prometheus instance={INSTANCE}。本看板：{eng_note}。"
    )
    d["templating"] = {
        "list": [
            {
                "current": {"text": INSTANCE, "value": INSTANCE},
                "hide": 2,
                "name": "instance",
                "options": [{"selected": True, "text": INSTANCE, "value": INSTANCE}],
                "query": INSTANCE,
                "type": "custom",
            }
        ]
    }
    if spec["engine"]:
        d["templating"]["list"].append(
            {
                "current": {"text": spec["engine"], "value": spec["engine"]},
                "hide": 2,
                "name": "engine",
                "options": [
                    {"selected": True, "text": spec["engine"], "value": spec["engine"]}
                ],
                "query": spec["engine"],
                "type": "custom",
            }
        )
    return d


def main() -> None:
    root = Path(__file__).parent
    base = root / "vllm-embed-vl-8601.base.json"
    if not base.exists():
        raise SystemExit("missing vllm-embed-vl-8601.base.json")

    # Normalize base template to DP3 instance (not legacy per-port targets).
    raw = json.loads(base.read_text(encoding="utf-8"))
    raw["description"] = (
        f"{MODEL} DP3 模板；生成时 instance 固定为 {INSTANCE}。"
    )
    for item in raw.get("templating", {}).get("list", []):
        if item.get("name") == "instance":
            item["current"] = {"text": INSTANCE, "value": INSTANCE}
            item["query"] = INSTANCE
            item["options"] = [{"selected": True, "text": INSTANCE, "value": INSTANCE}]
    base.write_text(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    for spec in VIEWS:
        dash = build_dashboard(base, spec)
        path = root / f"{spec['uid']}.json"
        path.write_text(json.dumps(dash, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("wrote", path.name, "->", spec["title"])


if __name__ == "__main__":
    main()
