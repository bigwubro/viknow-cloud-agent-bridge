#!/usr/bin/env python3
"""Push embed-vl dashboards to Grafana :8091 and fix Prometheus scrape (DP3 :8601 only)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DASHBOARDS = [
    {
        "uid": "vllm-embed-vl-8601",
        "slug": "vllm-qwen3-vl-embedding-2b-dp3-8601",
        "title": "vLLM Qwen3-VL-Embedding-2B · DP3 · :8601",
        "file": "vllm-embed-vl-8601.json",
    },
    {
        "uid": "vllm-embed-vl-8611",
        "slug": "vllm-qwen3-vl-embedding-2b-8611-dp3",
        "title": "vLLM Qwen3-VL-Embedding-2B · DP3 · 原 :8611 · engine 0 · GPU5",
        "file": "vllm-embed-vl-8611.json",
    },
    {
        "uid": "vllm-embed-vl-8612",
        "slug": "vllm-qwen3-vl-embedding-2b-8612-dp3",
        "title": "vLLM Qwen3-VL-Embedding-2B · DP3 · 原 :8612 · engine 2 · GPU7",
        "file": "vllm-embed-vl-8612.json",
    },
]

EMBED_SCRAPE = """  - targets:
    - 127.0.0.1:8601
    labels:
      model: Qwen/Qwen3-VL-Embedding-2B
      container: vllm-embed-vl
      port: '8601'
      data_parallel: '3'
"""


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, text=True)


def read_live_prom() -> str:
    return subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "/etc/prometheus/prometheus.yml:/cfg/prometheus.yml:ro",
            "alpine:3.20",
            "cat",
            "/cfg/prometheus.yml",
        ],
        text=True,
    )


def patch_prom(content: str) -> str:
    """Rewrite vllm job: shared targets + single :8601 DP3 scrape (no :8611/:8612)."""
    if "- job_name: vllm" not in content or "- job_name: lmcache" not in content:
        raise SystemExit("prometheus vllm/lmcache anchors not found")
    start = content.index("- job_name: vllm")
    end = content.index("- job_name: lmcache")
    head, tail = content[:start], content[end:]
    vllm = """- job_name: vllm
  metrics_path: /metrics
  static_configs:
  - targets:
    - 127.0.0.1:8500
    - 127.0.0.1:8506
    - 127.0.0.1:8501
    - 127.0.0.1:8602
""" + EMBED_SCRAPE
    return head + vllm + tail


def write_prom(content: str) -> None:
    tmp = ROOT / "prometheus.embed-vl.yml"
    tmp.write_text(content, encoding="utf-8")
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{tmp}:/src/prometheus.yml:ro",
            "-v",
            "/etc/prometheus/prometheus.yml:/cfg/prometheus.yml",
            "alpine:3.20",
            "sh",
            "-c",
            "cat /src/prometheus.yml > /cfg/prometheus.yml",
        ]
    )
    run(["docker", "restart", "prometheus_grafana-prometheus-1"])
    for _ in range(25):
        time.sleep(1)
        p = subprocess.run(
            ["curl", "-sS", "-m", "2", "http://127.0.0.1:9099/-/healthy"],
            capture_output=True,
            text=True,
        )
        if p.returncode == 0 and "Healthy" in p.stdout:
            return
    raise SystemExit("prometheus not healthy")


def sql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def apply_grafana() -> None:
    stmts = [
        "BEGIN;",
        "DELETE FROM dashboard_version WHERE dashboard_id IN (SELECT id FROM dashboard WHERE uid IN ('vllm-embed-vl-8601','vllm-embed-vl-8611','vllm-embed-vl-8612'));",
        "DELETE FROM dashboard WHERE uid IN ('vllm-embed-vl-8601','vllm-embed-vl-8611','vllm-embed-vl-8612');",
    ]
    for spec in DASHBOARDS:
        data = json.loads((ROOT / spec["file"]).read_text(encoding="utf-8"))
        data["title"] = spec["title"]
        data["uid"] = spec["uid"]
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        stmts.append(
            "INSERT INTO dashboard (version, slug, title, data, org_id, created, updated, "
            "updated_by, created_by, folder_id, is_folder, has_acl, uid, is_public) VALUES ("
            f"1, {sql_quote(spec['slug'])}, {sql_quote(spec['title'])}, {sql_quote(payload)}, "
            "1, datetime('now'), datetime('now'), 1, 1, 0, 0, 0, "
            f"{sql_quote(spec['uid'])}, 0);"
        )
        stmts.append(
            "INSERT INTO dashboard_version (dashboard_id, parent_version, restored_from, version, "
            "created, created_by, message, data) VALUES ("
            "last_insert_rowid(), 0, 0, 1, datetime('now'), 1, "
            f"{sql_quote('embed-vl DP3')}, {sql_quote(payload)});"
        )
        stmts.append(
            "UPDATE dashboard SET data = json_set(data, '$.id', id, '$.version', 1) "
            f"WHERE uid = {sql_quote(spec['uid'])};"
        )
    stmts.append("COMMIT;")
    sql_path = ROOT / "apply.sql"
    sql_path.write_text("\n".join(stmts) + "\n", encoding="utf-8")
    run(["docker", "cp", str(sql_path), "prometheus_grafana-grafana-1:/tmp/apply-embed-vl.sql"])
    run(
        [
            "docker",
            "exec",
            "prometheus_grafana-grafana-1",
            "sqlite3",
            "/var/lib/grafana/grafana.db",
            ".read /tmp/apply-embed-vl.sql",
        ]
    )


def main() -> None:
    live = read_live_prom()
    new = patch_prom(live)
    if new != live:
        write_prom(new)
    else:
        print("prometheus.yml unchanged")
    time.sleep(6)
    apply_grafana()
    print("done")


if __name__ == "__main__":
    main()
