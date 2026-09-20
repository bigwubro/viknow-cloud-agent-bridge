#!/usr/bin/env python3
"""Build the Qwen3.6 3P+1D Grafana dashboard.

Same uid as the old「一个实例」board. Every chart is a P | D pair on one dashboard.
Queries go to prometheus-qwen36 (:9108), which scrapes :8510/:8511/:8512/:8513.
:8500 is excluded so nginx is not double-counted.
"""
from __future__ import annotations

import json
from pathlib import Path

UID = "b281712d-8bff-41ef-9f3f-71ad43c05e9b"
DS = {"type": "prometheus", "uid": "efxtwpht5answc"}
MODEL = '$model_name'
# Instance list, not leftover 1P+1D role labels (8511 used to be tagged decode).
P = (
    f'job="vllm_qwen36_pd_engines",model_name="{MODEL}",'
    'instance=~"127.0.0.1:8510|127.0.0.1:8511|127.0.0.1:8512"'
)
D = f'job="vllm_qwen36_pd_engines",model_name="{MODEL}",instance="127.0.0.1:8513"'
GPU_P = 'job="dcgm_qwen36_gpu0_3",gpu=~"0|1|2"'
GPU_D = 'job="dcgm_qwen36_gpu0_3",gpu="3"'

# rate(sum)/rate(count) + min samples. increase(sum)/increase(count) spikes to
# "days" when counters reset on restart-pd (negative queue avg is the same bug).
def avg_latency(metric: str, filter_expr: str, min_increase: str = "0.5") -> str:
    return (
        f"((sum(rate({metric}_sum{{{filter_expr}}}[$__rate_interval])) "
        f"/ clamp_min(sum(rate({metric}_count{{{filter_expr}}}[$__rate_interval])), 1e-9)) "
        f"and on() (sum(increase({metric}_count{{{filter_expr}}}[$__rate_interval])) > {min_increase}))"
    )


NEXT_ID = 1


def nid() -> int:
    global NEXT_ID
    i = NEXT_ID
    NEXT_ID += 1
    return i


def row(title: str, y: int) -> dict:
    return {
        "id": nid(),
        "type": "row",
        "title": title,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "collapsed": False,
        "panels": [],
    }


def stat(title: str, expr: str, y: int, x: int, *, unit: str = "short", decimals: int | None = 0, color: str = "green", w: int = 3) -> dict:
    fc: dict = {
        "defaults": {
            "decimals": decimals,
            "unit": unit,
            "color": {"mode": "fixed", "fixedColor": color},
        }
    }
    return {
        "id": nid(),
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "fieldConfig": fc,
        "options": {
            "colorMode": "background",
            "graphMode": "area",
            "reduceOptions": {"calcs": ["lastNotNull"]},
        },
        "targets": [{"expr": expr, "legendFormat": title, "refId": "A", "datasource": DS}],
    }


def ts(
    title: str,
    targets: list[tuple[str, str]],
    y: int,
    x: int = 0,
    *,
    w: int = 12,
    h: int = 8,
    unit: str = "short",
    desc: str = "",
    ymax=None,
    overrides: list | None = None,
) -> dict:
    tgs = []
    for i, (expr, legend) in enumerate(targets):
        tgs.append(
            {
                "expr": expr,
                "legendFormat": legend,
                "refId": chr(65 + i),
                "datasource": DS,
                "editorMode": "code",
                "range": True,
            }
        )
    defaults = {
        "custom": {
            "drawStyle": "line",
            "lineWidth": 2,
            "fillOpacity": 12,
            "spanNulls": True,
            "showPoints": "never",
            "lineInterpolation": "linear",
        },
        "min": 0,
        "unit": unit,
    }
    if ymax is not None:
        defaults["max"] = ymax
    return {
        "id": nid(),
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"]},
            "tooltip": {"mode": "multi"},
        },
        "targets": tgs,
    }


def quantile_set(metric: str, sel: str) -> list[tuple[str, str]]:
    avg = (
        f"(sum(increase({metric}_sum{{{sel}}}[$__rate_interval])) "
        f"/ clamp_min(sum(increase({metric}_count{{{sel}}}[$__rate_interval])), 1e-9))"
    )
    enough = f"sum(increase({metric}_count{{{sel}}}[$__rate_interval])) > 8"
    out = [(avg, "avg")]
    for q, name in ((0.5, "p50"), (0.9, "p90"), (0.95, "p95")):
        expr = (
            f"histogram_quantile({q}, sum by(le) (increase({metric}_bucket{{{sel}}}[$__rate_interval]))) "
            f"and on() ({enough})"
        )
        out.append((expr, name))
    return out


def color_override(name: str, color: str) -> dict:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color", "value": {"fixedColor": color, "mode": "fixed"}}],
    }


def build_dashboard() -> dict:
    global NEXT_ID
    NEXT_ID = 1
    panels: list[dict] = []
    y = 0

    panels.append(row("总览 · 左 P（三张预填充）　右 D（一张出词）　不含 :8500", y))
    y += 1
    panels += [
        stat("P 引擎", f'count(vllm:num_requests_running{{{P}}})', y, 0, color="green"),
        stat("P running", f'sum(vllm:num_requests_running{{{P}}})', y, 3, color="green"),
        stat("P waiting", f'sum(vllm:num_requests_waiting{{{P}}})', y, 6, color="orange"),
        stat("P KV", f'avg(vllm:kv_cache_usage_perc{{{P}}})', y, 9, unit="percentunit", decimals=2, color="yellow"),
        stat("P prompt tok/s", f'sum(rate(vllm:prompt_tokens_total{{{P}}}[$__rate_interval]))', y, 12, unit="cps", decimals=0, color="green"),
        stat("D 引擎", f'count(vllm:num_requests_running{{{D}}})', y, 15, color="purple"),
        stat("D running", f'sum(vllm:num_requests_running{{{D}}})', y, 18, color="purple"),
        stat("D waiting", f'sum(vllm:num_requests_waiting{{{D}}})', y, 21, color="super-light-red"),
    ]
    y += 4
    panels += [
        stat("D KV", f'avg(vllm:kv_cache_usage_perc{{{D}}})', y, 0, unit="percentunit", decimals=2, color="purple", w=6),
        stat("D decode tok/s", f'sum(rate(vllm:generation_tokens_total{{{D}}}[$__rate_interval]))', y, 6, unit="cps", decimals=0, color="purple", w=6),
        stat("D 成功 QPS", f'sum(rate(vllm:request_success_total{{{D}}}[$__rate_interval]))', y, 12, unit="reqps", decimals=2, color="purple", w=6),
        stat("P 成功 QPS", f'sum(rate(vllm:request_success_total{{{P}}}[$__rate_interval]))', y, 18, unit="reqps", decimals=2, color="green", w=6),
    ]
    y += 4

    panels.append(row("调度队列", y))
    y += 1

    def sched_targets(sel: str):
        return [
            (f"sum by (service) (vllm:num_requests_running{{{sel}}})", "running {{service}}"),
            (f'sum by (service) (vllm:num_requests_waiting_by_reason{{{sel},reason="capacity"}})', "waiting {{service}}"),
            (f'sum by (service) (vllm:num_requests_waiting_by_reason{{{sel},reason="deferred"}})', "deferred {{service}}"),
        ]

    panels.append(
        ts(
            "P 调度（p0 / p1 / p2）",
            sched_targets(P),
            y,
            0,
            desc="三张预填充卡各自的 running / waiting / deferred。",
            overrides=[
                color_override("running qwen36-p0", "green"),
                color_override("running qwen36-p1", "semi-dark-green"),
                color_override("running qwen36-p2", "super-light-green"),
            ],
        )
    )
    panels.append(
        ts(
            "D 调度（d3）",
            sched_targets(D),
            y,
            12,
            desc="出词卡 running / waiting / deferred。WAITING_FOR_REMOTE_KVS 会计入 deferred。",
            overrides=[
                color_override("running qwen36-d3", "purple"),
                color_override("waiting qwen36-d3", "red"),
                color_override("deferred qwen36-d3", "orange"),
            ],
        )
    )
    y += 8

    panels.append(row("吞吐 tok/s", y))
    y += 1
    panels.append(
        ts(
            "P prompt tok/s（按引擎）",
            [(f'sum by (service) (rate(vllm:prompt_tokens_total{{{P}}}[$__rate_interval]))', "{{service}}")],
            y,
            0,
            unit="cps",
            desc="预填充侧新算的 prompt token/s。",
        )
    )
    panels.append(
        ts(
            "D decode tok/s",
            [(f'sum by (service) (rate(vllm:generation_tokens_total{{{D}}}[$__rate_interval]))', "{{service}}")],
            y,
            12,
            unit="cps",
            desc="出词侧生成 token/s。MTP 接受的也算在这里。",
        )
    )
    y += 8

    panels.append(row("端到端与分段时延", y))
    y += 1
    panels.append(
        ts(
            "P E2E（预填充侧，少样本只画平均）",
            quantile_set("vllm:e2e_request_latency_seconds", P),
            y,
            0,
            unit="s",
            desc="P 引擎完成一次预填充的墙钟。不含出词。",
        )
    )
    panels.append(
        ts(
            "D E2E（出词侧，少样本只画平均）",
            quantile_set("vllm:e2e_request_latency_seconds", D),
            y,
            12,
            unit="s",
            desc="D 引擎完成一次出词的墙钟，含等 KV。",
        )
    )
    y += 8
    panels.append(
        ts(
            "P 排队 / 预填充（平均）",
        [
            (avg_latency("vllm:request_queue_time_seconds", P), "queue avg"),
            (avg_latency("vllm:request_prefill_time_seconds", P), "prefill avg"),
        ],
        y,
        0,
        unit="s",
        desc="短窗 rate(sum)/rate(count)。勿用 increase 做平均：restart-pd 后会出现「几天」假峰与负 queue。",
        )
    )
    panels.append(
        ts(
            "D 排队 / 出词 / NIXL（平均）",
            [
                (avg_latency("vllm:request_queue_time_seconds", D), "queue avg"),
                (avg_latency("vllm:request_decode_time_seconds", D), "decode avg"),
                (
                    avg_latency("vllm:nixl_xfer_time_seconds", D, min_increase="0.1"),
                    "nixl avg",
                ),
            ],
            y,
            12,
            unit="s",
            desc="出词卡三段：等调度、算 184 token、拉 KV。",
        )
    )
    y += 8
    panels.append(
        ts(
            "P TTFT（若有）",
            quantile_set("vllm:time_to_first_token_seconds", P),
            y,
            0,
            unit="s",
        )
    )
    panels.append(
        ts(
            "D TTFT / ITL",
            quantile_set("vllm:time_to_first_token_seconds", D)[:2]
            + [
                (
                    f"(sum(increase(vllm:inter_token_latency_seconds_sum{{{D}}}[$__rate_interval])) "
                    f"/ clamp_min(sum(increase(vllm:inter_token_latency_seconds_count{{{D}}}[$__rate_interval])), 1e-9))",
                    "ITL avg",
                )
            ],
            y,
            12,
            unit="s",
        )
    )
    y += 8

    panels.append(row("KV · 前缀缓存 · MTP", y))
    y += 1
    panels.append(
        ts(
            "P KV 占用",
            [(f'vllm:kv_cache_usage_perc{{{P}}}', "{{service}}")],
            y,
            0,
            unit="percentunit",
            ymax=1,
        )
    )
    panels.append(
        ts(
            "D KV 占用",
            [(f'vllm:kv_cache_usage_perc{{{D}}}', "{{service}}")],
            y,
            12,
            unit="percentunit",
            ymax=1,
        )
    )
    y += 8
    panels.append(
        ts(
            "P prefix / Mooncake hit",
            [
                (
                    f"sum(rate(vllm:prefix_cache_hits_total{{{P}}}[$__rate_interval])) "
                    f"/ clamp_min(sum(rate(vllm:prefix_cache_queries_total{{{P}}}[$__rate_interval])), 1e-9)",
                    "gpu prefix",
                ),
                (
                    f"sum(rate(vllm:external_prefix_cache_hits_total{{{P}}}[$__rate_interval])) "
                    f"/ clamp_min(sum(rate(vllm:external_prefix_cache_queries_total{{{P}}}[$__rate_interval])), 1e-9)",
                    "external / Mooncake",
                ),
            ],
            y,
            0,
            unit="percentunit",
            ymax=1,
        )
    )
    panels.append(
        ts(
            "D MTP 接受率 / NIXL 次数",
            [
                (
                    f"sum(rate(vllm:spec_decode_num_accepted_tokens_total{{{D}}}[$__rate_interval])) "
                    f"/ clamp_min(sum(rate(vllm:spec_decode_num_draft_tokens_total{{{D}}}[$__rate_interval])), 1e-9)",
                    "MTP accept",
                ),
                (
                    f"sum(rate(vllm:nixl_xfer_time_seconds_count{{{D}}}[$__rate_interval]))",
                    "NIXL xfer/s",
                ),
            ],
            y,
            12,
        )
    )
    y += 8
    panels.append(
        ts(
            "P 抢占 /s",
            [(f'sum by (service) (rate(vllm:num_preemptions_total{{{P}}}[$__rate_interval]))', "{{service}}")],
            y,
            0,
        )
    )
    panels.append(
        ts(
            "D 抢占 /s",
            [(f'sum by (service) (rate(vllm:num_preemptions_total{{{D}}}[$__rate_interval]))', "{{service}}")],
            y,
            12,
        )
    )
    y += 8

    panels.append(row("Finish reason · 长度", y))
    y += 1
    panels.append(
        ts(
            "P finish reason",
            [(f'sum by (finished_reason) (increase(vllm:request_success_total{{{P}}}[$__rate_interval]))', "{{finished_reason}}")],
            y,
            0,
        )
    )
    panels.append(
        ts(
            "D finish reason",
            [(f'sum by (finished_reason) (increase(vllm:request_success_total{{{D}}}[$__rate_interval]))', "{{finished_reason}}")],
            y,
            12,
        )
    )
    y += 8

    panels.append(row("硬件 GPU0–2 = P　GPU3 = D", y))
    y += 1
    panels.append(
        ts(
            "P Tensor Active（GPU0–2）",
            [(f"DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{{{GPU_P}}}", "gpu {{gpu}}")],
            y,
            0,
            unit="percentunit",
            ymax=1,
        )
    )
    panels.append(
        ts(
            "D Tensor Active（GPU3）",
            [(f"DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{{{GPU_D}}}", "gpu {{gpu}}")],
            y,
            12,
            unit="percentunit",
            ymax=1,
        )
    )
    y += 8
    panels.append(
        ts(
            "P Power W（GPU0–2）",
            [(f"DCGM_FI_DEV_POWER_USAGE{{{GPU_P}}}", "gpu {{gpu}}")],
            y,
            0,
            unit="watt",
        )
    )
    panels.append(
        ts(
            "D Power W（GPU3）",
            [(f"DCGM_FI_DEV_POWER_USAGE{{{GPU_D}}}", "gpu {{gpu}}")],
            y,
            12,
            unit="watt",
        )
    )
    y += 8
    panels.append(
        ts(
            "P / D 温度 °C",
            [
                (f"DCGM_FI_DEV_GPU_TEMP{{{GPU_P}}}", "P gpu {{gpu}}"),
                (f"DCGM_FI_DEV_GPU_TEMP{{{GPU_D}}}", "D gpu {{gpu}}"),
            ],
            y,
            0,
            w=24,
            unit="celsius",
        )
    )
    y += 8

    panels.append(
        {
            "id": nid(),
            "type": "text",
            "title": "口径",
            "gridPos": {"h": 5, "w": 24, "x": 0, "y": y},
            "options": {
                "mode": "markdown",
                "content": (
                    "同一张看板，图按 **P / D** 拆开。数据源是 `prometheus-qwen36`（:9108），"
                    "只取 `job=vllm_qwen36_pd_engines`。\n\n"
                    "- **P**：`:8510` p0、`:8511` p1、`:8512` p2，`role=prefill`。"
                    "E2E 是预填充墙钟，不含出词。\n"
                    "- **D**：`:8513` d3，`role=decode`。E2E 含等 KV 和 184 个 token。\n"
                    "- **不含** `:8500` nginx，避免和引擎重复计数。\n"
                    "- GPU0–2 画在 P，GPU3 画在 D。\n"
                    "- 现网 3P+1D：P token 预算 65536 / 20 GiB KV，D 16384。"
                ),
            },
        }
    )

    return {
        "annotations": {"list": []},
        "description": "原「一个实例」看板按 3P+1D 对齐：同一 dashboard，图按 P / D 分开。数据源 prometheus-qwen36 :9108。",
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "panels": panels,
        "refresh": "5s",
        "schemaVersion": 39,
        "tags": ["qwen36", "vllm", "3p1d", "prefill", "decode"],
        "templating": {
            "list": [
                {
                    "current": {"text": "prometheus-qwen36", "value": "efxtwpht5answc"},
                    "hide": 2,
                    "name": "DS_PROMETHEUS",
                    "query": "prometheus",
                    "type": "datasource",
                },
                {
                    "current": {"text": "Qwen/Qwen3.6-35B-A3B-FP8", "value": "Qwen/Qwen3.6-35B-A3B-FP8"},
                    "hide": 2,
                    "name": "model_name",
                    "query": "Qwen/Qwen3.6-35B-A3B-FP8",
                    "type": "constant",
                    "skipUrlSync": True,
                },
            ]
        },
        "time": {"from": "now-30m", "to": "now"},
        "timezone": "browser",
        "title": "vLLM · Qwen3.6-35B · 3P+1D",
        "uid": UID,
        "version": 36,
    }


def main() -> None:
    out = Path(__file__).with_name("qwen36-3p1d-dashboard.json")
    dash = build_dashboard()
    out.write_text(json.dumps(dash, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} panels={len(dash['panels'])}")


if __name__ == "__main__":
    main()
