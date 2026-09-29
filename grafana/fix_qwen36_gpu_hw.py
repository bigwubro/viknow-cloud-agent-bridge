#!/usr/bin/env python3
"""Patch Grafana dashboard b281712d hardware row (run against grafana.db copy)."""
import json
import sqlite3
import sys

UID = "b281712d-8bff-41ef-9f3f-71ad43c05e9b"
DB = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/grafana/grafana.db"
DS = {"type": "prometheus", "uid": "efxtwpht5answc"}
JOB = "dcgm_qwen36_gpu0_3"


def walk(ps):
    for p in ps:
        yield p
        for c in p.get("panels") or []:
            yield from walk([c])


def panel_by_id(panels, pid):
    for p in walk(panels):
        if p.get("id") == pid:
            return p
    return None


def ts_defaults(min_v, max_v, unit):
    return {
        "min": min_v,
        "max": max_v,
        "unit": unit,
        "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 12},
    }


conn = sqlite3.connect(DB)
dash_id, data, version = conn.execute(
    "SELECT id, data, version FROM dashboard WHERE uid=?", (UID,)
).fetchone()
dash = json.loads(data)
panels = dash["panels"]

for pid, spec in (
    (
        60,
        {
            "title": "Qwen3.6 GPU Util（时间忙闲 %）",
            "expr": f'DCGM_FI_DEV_GPU_UTIL{{job="{JOB}"}}',
            "legend": "GPU{{gpu}} util %",
            "grid": {"h": 8, "w": 12, "x": 0, "y": 78},
            "fc": ts_defaults(0, 100, "percent"),
        },
    ),
    (
        61,
        {
            "title": "Qwen3.6 Tensor Active（算力 / Tensor Core）",
            "expr": f'DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{{job="{JOB}"}}',
            "legend": "GPU{{gpu}} tensor active",
            "grid": {"h": 8, "w": 12, "x": 12, "y": 78},
            "fc": ts_defaults(0, 1, "percentunit"),
        },
    ),
):
    p = panel_by_id(panels, pid)
    if not p:
        continue
    p.update(
        {
            "title": spec["title"],
            "gridPos": spec["grid"],
            "datasource": DS,
            "fieldConfig": {"defaults": spec["fc"]},
            "targets": [
                {"expr": spec["expr"], "legendFormat": spec["legend"], "refId": "A"}
            ],
        }
    )

p62 = panel_by_id(panels, 62)
if p62:
    p62.update(
        {
            "title": "Qwen3.6 Mem Copy Util（显存拷贝 %）",
            "gridPos": {"h": 8, "w": 12, "x": 0, "y": 86},
            "datasource": DS,
            "fieldConfig": {"defaults": ts_defaults(0, 100, "percent")},
            "targets": [
                {
                    "expr": f'DCGM_FI_DEV_MEM_COPY_UTIL{{job="{JOB}"}}',
                    "legendFormat": "GPU{{gpu}} mem copy %",
                    "refId": "A",
                }
            ],
        }
    )

if not panel_by_id(panels, 63):
    panels.append(
        {
            "id": 63,
            "type": "timeseries",
            "title": "Qwen3.6 SM Active（SM 上有 warp）",
            "gridPos": {"h": 8, "w": 12, "x": 12, "y": 86},
            "datasource": DS,
            "fieldConfig": {"defaults": ts_defaults(0, 1, "percentunit")},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
            "targets": [
                {
                    "expr": f'DCGM_FI_PROF_SM_ACTIVE{{job="{JOB}"}}',
                    "legendFormat": "GPU{{gpu}} SM active",
                    "refId": "A",
                }
            ],
        }
    )

dash["version"] = (dash.get("version") or 0) + 1
conn.execute(
    "UPDATE dashboard SET data=?, version=?, updated=datetime('now') WHERE id=?",
    (json.dumps(dash, ensure_ascii=False, separators=(",", ":")), version + 1, dash_id),
)
conn.commit()
conn.close()
print("ok", UID, "db", DB)
