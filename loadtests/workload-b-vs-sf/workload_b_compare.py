#!/usr/bin/env python3
"""Workload B only: shared-prefix ~30k vs SiliconFlow token bill.

Default commands (plan / estimate / report --demo-morning) do not send
chat completions. `run --target sf` requires --i-accept-sf-cost.
Does not change :8500. Does not hit :5180.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import string
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

# --- dataset B: same strings as direct-llm-80x20m-20260917 ---
LINE = "检索片段：字幕 figs but I still would round up so I，画面底部有粉色横条和一条斜线，两人在讲解题目。\n"
HEAD = (
    "你是视频知识库助手。根据检索到的字幕和画面描述回答选择题。\n"
    "问题：Two women on the screen are explaining a question. In the problem, "
    "there is a number crossing a pink bar at the bottom and a slanted line. "
    "When the subtitle 'figs but I still would round up so I' appears, what color is the slanted line?\n"
    "选项：A. Olive Yellow B. Light Yellow C. Light Blue D. Grass Green E. Ink Green\n"
    "请根据下列检索上下文作答，末行写：最终选择：X\n"
)

# Morning calibrate (TP4 B run). Used for plan/estimate when we do not tokenize.
MORNING = {
    "shared_tokenize": 10561,
    "unique_tokenize": 8382,
    "probe_prompt_tokens": 18954,
    "prompt_tokens_avg": 20877.748742138363,
    "completion_tokens_avg": 183.9990566037736,
    "done": 3180,
    "duration_sec": 1200,
    "qps": 3180 / 1200,
    "e2e_p50": 29.41,
    "e2e_avg": 30.404488827522446,
    "prefix_hit": 0.505,
}

LOCAL_BASE = os.environ.get("LLM_BASE", "http://127.0.0.1:8500").rstrip("/")
LOCAL_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen3.6-35B-A3B-FP8")
SF_BASE = os.environ.get("SF_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
SF_MODEL = os.environ.get("SF_MODEL", "Qwen/Qwen3.6-35B-A3B")
SF_KEY = os.environ.get("SILICONFLOW_API_KEY", "")

# 3万 prompt；独特尾巴按早上 B 的 8500/19000 ≈ 45% 同比拉长，共享约 55% 可缓存。
TARGET_PROMPT = int(os.environ.get("LLM_TARGET_PROMPT_TOKENS", "30000"))
UNIQUE_TOKENS = int(os.environ.get("LLM_UNIQUE_TOKENS", "13500"))
# 早上 19k 目标 → usage 20878，chat template 大约 ×1.10
USAGE_OVERHEAD = MORNING["prompt_tokens_avg"] / 19000.0
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "184"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT_SEC", "300"))
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "60"))

SF_IN = float(os.environ.get("SF_CNY_PER_M_IN", "1.80"))
SF_OUT = float(os.environ.get("SF_CNY_PER_M_OUT", "10.80"))
SF_CACHE = os.environ.get("SF_CACHE_CNY_PER_M", "")  # empty = treat cache as full input
SF_BUDGET = float(os.environ.get("SF_BUDGET_CNY", "80"))

PROM = os.environ.get("PROM_URL", "http://127.0.0.1:9099")
PROM_DCGM = os.environ.get("PROM_DCGM_URL", "http://127.0.0.1:9108")

CAPEX = {
    "low": float(os.environ.get("CAPEX_LOW_CNY", "360000")),
    "high": float(os.environ.get("CAPEX_HIGH_CNY", "440000")),
}
YUAN_KWH = float(os.environ.get("YUAN_PER_KWH", "1.0"))

# Named TCO scenarios (capex, years, util, kW)
TCO_SCENARIOS = [
    ("mid_36w_3y_60_2kW", CAPEX["low"], 3, 0.60, 2.0),
    ("dear_idle_44w_2y_30_2k4", CAPEX["high"], 2, 0.30, 2.4),
    ("cheap_full_36w_3y_90_1k6", CAPEX["low"], 3, 0.90, 1.6),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return ys[lo]
    return ys[lo] * (hi - k) + ys[hi] * (k - lo)


def hours_year(years: float, util: float) -> float:
    return years * 365.25 * 24.0 * util


def tco_hourly(capex: float, years: float, util: float, kw: float) -> dict[str, float]:
    amort = capex / hours_year(years, util)
    power = kw * YUAN_KWH
    return {"amort_cny_h": amort, "power_cny_h": power, "total_cny_h": amort + power}


def tco_per_req(hourly_cny: float, qps: float) -> float | None:
    """QPS is requests/second; one hour completes qps*3600 requests."""
    if not qps:
        return None
    return hourly_cny / (qps * 3600.0)


def _assert_tco_math() -> None:
    # 36万 / 3年 / 60% / 2.0kW @ 2.65 QPS ≈ ¥0.0026/req, not ¥9.36 (forgot *3600)
    h = tco_hourly(360000, 3, 0.60, 2.0)
    per = tco_per_req(h["total_cny_h"], 2.65)
    assert 24.0 < h["total_cny_h"] < 26.0, h
    assert per is not None and 0.0024 < per < 0.0028, per
    bill = sf_bill(20877.75, 184.0)
    assert 0.039 < bill["total_cny"] < 0.041, bill


def sf_bill(prompt: float, completion: float, cached: float = 0.0) -> dict[str, float]:
    cached = min(cached, prompt)
    miss = max(prompt - cached, 0.0)
    cache_price = float(SF_CACHE) if SF_CACHE else SF_IN
    return {
        "in_cny": miss / 1e6 * SF_IN + cached / 1e6 * cache_price,
        "out_cny": completion / 1e6 * SF_OUT,
        "total_cny": miss / 1e6 * SF_IN + cached / 1e6 * cache_price + completion / 1e6 * SF_OUT,
    }


def http_json(
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:800]
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"error": body}
        return exc.code, parsed


def tokenize_local(text: str) -> int:
    code, payload = http_json(LOCAL_BASE + "/tokenize", {"prompt": text}, timeout=30)
    if code != 200:
        raise RuntimeError(f"tokenize failed {code}: {payload}")
    return int(payload["count"])


def build_shared(shared_path: Path | None = None) -> tuple[str, int]:
    shared_target = max(200, TARGET_PROMPT - UNIQUE_TOKENS)
    if shared_path and shared_path.exists():
        text = shared_path.read_text(encoding="utf-8")
        tok = tokenize_local(text)
        if abs(tok - shared_target) <= 200:
            return text, tok
    text = HEAD
    n = 1
    while tokenize_local(text) < shared_target:
        text += LINE * 40
        n += 1
        if n > 80:
            break
    while tokenize_local(text) > shared_target + 80 and text.count(LINE) > 0:
        text = text[: text.rfind(LINE)]
    return text, tokenize_local(text)


def unique_block(tag: str) -> str:
    n = max(8, UNIQUE_TOKENS // 44)
    return "".join(f"本请求检索编号 {tag} 第{i}段：" + LINE for i in range(n))


def cached_from_usage(usage: dict) -> float:
    if not usage:
        return 0.0
    for key in ("cached_tokens", "prompt_cache_hit_tokens"):
        if usage.get(key) is not None:
            return float(usage[key])
    details = usage.get("prompt_tokens_details") or {}
    for key in ("cached_tokens", "cached_tokens_total", "cache_read_input_tokens"):
        if details.get(key) is not None:
            return float(details[key])
    return 0.0


def chat_body(model: str, content: str, max_tokens: int) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


class Budget:
    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.spent = 0.0
        self.lock = threading.Lock()

    def add(self, cny: float) -> bool:
        with self.lock:
            self.spent += cny
            return self.spent <= self.cap

    def ok(self) -> bool:
        with self.lock:
            return self.spent <= self.cap


def one_call(
    *,
    target: str,
    content: str,
    worker: int,
    seq: int,
    tag: str,
    timeout: float,
    budget: Budget | None,
) -> dict:
    if target == "local":
        url = LOCAL_BASE + "/v1/chat/completions"
        headers = {}
        model = LOCAL_MODEL
    else:
        url = SF_BASE + "/chat/completions"
        headers = {"Authorization": f"Bearer {SF_KEY}"}
        model = SF_MODEL
    body = chat_body(model, content, MAX_TOKENS)
    t0 = time.time()
    status = "error"
    err = None
    usage: dict = {}
    finish = None
    http_status = None
    retries_429 = 0
    for attempt in range(6):
        http_status, payload = http_json(url, body, headers=headers, timeout=timeout)
        if http_status == 429:
            retries_429 += 1
            time.sleep(min(30, 2**attempt))
            continue
        if http_status == 200:
            usage = payload.get("usage") or {}
            finish = (payload.get("choices") or [{}])[0].get("finish_reason")
            status = "ok"
        else:
            err = f"http_{http_status}: {payload}"
        break
    else:
        err = "http_429: retries exhausted"
    elapsed = time.time() - t0
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    cached = cached_from_usage(usage)
    bill = sf_bill(float(pt or 0), float(ct or 0), cached) if target == "sf" else None
    if bill and budget is not None:
        budget.add(bill["total_cny"])
    return {
        "ts": utc_now(),
        "target": target,
        "worker": worker,
        "seq": seq,
        "tag": tag,
        "status": status,
        "http_status": http_status,
        "retries_429": retries_429,
        "elapsed_sec": round(elapsed, 3),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cached_tokens": cached,
        "finish_reason": finish,
        "sf_cny": None if bill is None else round(bill["total_cny"], 6),
        "error": err,
    }


def prom_query(base: str, expr: str) -> list[dict]:
    url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        _, payload = http_json(url, timeout=10)
    except Exception as exc:
        return [{"error": str(exc)}]
    out = []
    for s in (payload.get("data") or {}).get("result", []):
        val = s.get("value", [None, None])
        try:
            num = float(val[1])
        except Exception:
            num = None
        out.append({"metric": s.get("metric", {}), "value": num})
    return out


def prom_sum(base: str, expr: str) -> float | None:
    series = prom_query(base, expr)
    vals = [x["value"] for x in series if isinstance(x, dict) and x.get("value") is not None]
    if not vals:
        return None
    return sum(vals)


SCRAPE_EXPRS = {
    "running": "sum(vllm:num_requests_running)",
    "waiting": "sum(vllm:num_requests_waiting)",
    "prompt_total": "sum(vllm:prompt_tokens_total)",
    "prompt_cached": "sum(vllm:prompt_tokens_cached_total)",
    "prompt_recomputed": "sum(vllm:prompt_tokens_recomputed_total)",
    "prefix_hits": "sum(vllm:prefix_cache_hits_total)",
    "prefix_queries": "sum(vllm:prefix_cache_queries_total)",
    "computed_prefill_sum": "sum(vllm:request_prefill_kv_computed_tokens_sum)",
    "gen_total": "sum(vllm:generation_tokens_total)",
}


def scrape_once() -> dict:
    row: dict[str, Any] = {"ts": utc_now(), "t": time.time()}
    for name, expr in SCRAPE_EXPRS.items():
        row[name] = prom_sum(PROM, expr)
    row["power_w_sum"] = prom_sum(PROM_DCGM, 'sum(DCGM_FI_DEV_POWER_USAGE{job="dcgm_qwen36_gpu0_3"})')
    if row["power_w_sum"] is None:
        row["power_w_sum"] = prom_sum(PROM_DCGM, "sum(DCGM_FI_DEV_POWER_USAGE)")
    row["gpu_util_avg"] = prom_sum(PROM_DCGM, 'avg(DCGM_FI_DEV_GPU_UTIL{job="dcgm_qwen36_gpu0_3"})')
    return row


def scrape_loop(path: Path, stop: threading.Event, interval: float = 5.0) -> None:
    while not stop.is_set():
        row = scrape_once()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        stop.wait(interval)


def summarize_metrics(path: Path) -> dict:
    if not path.exists():
        return {}
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(rows) < 2:
        return {"samples": len(rows)}

    def delta(key: str) -> float | None:
        xs = [r.get(key) for r in rows if r.get(key) is not None]
        if len(xs) < 2:
            return None
        return xs[-1] - xs[0]

    d_hits = delta("prefix_hits")
    d_q = delta("prefix_queries")
    d_prompt = delta("prompt_total")
    d_cached = delta("prompt_cached")
    powers = [r["power_w_sum"] for r in rows if r.get("power_w_sum") is not None]
    return {
        "samples": len(rows),
        "prefix_hit": (d_hits / d_q) if d_hits is not None and d_q else None,
        "prompt_tokens_delta": d_prompt,
        "prompt_cached_delta": d_cached,
        "prompt_recomputed_delta": delta("prompt_recomputed"),
        "computed_prefill_delta": delta("computed_prefill_sum"),
        "gen_tokens_delta": delta("gen_total"),
        "power_w_avg": (mean(powers) if powers else None),
        "running_avg": mean([r["running"] for r in rows if r.get("running") is not None] or [0]),
        "waiting_avg": mean([r["waiting"] for r in rows if r.get("waiting") is not None] or [0]),
    }


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, obj: dict, lock: threading.Lock) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)


def run_level(
    *,
    out_dir: Path,
    target: str,
    concurrency: int,
    duration: int,
    shared: str,
    accept_sf: bool,
) -> dict:
    if target == "sf":
        if not accept_sf:
            raise SystemExit("sf run refused: pass --i-accept-sf-cost")
        if not SF_KEY:
            raise SystemExit("sf run refused: SILICONFLOW_API_KEY is empty")
    level_dir = out_dir / f"{target}_c{concurrency}"
    level_dir.mkdir(parents=True, exist_ok=True)
    runs_path = level_dir / "runs.jsonl"
    events_path = level_dir / "loadgen_events.jsonl"
    metrics_path = level_dir / "metrics.jsonl"
    lock = threading.Lock()
    stop_at = 0.0
    budget = Budget(SF_BUDGET) if target == "sf" else None
    submitted = 0
    in_flight = 0

    meta = {
        "started_at": utc_now(),
        "target": target,
        "concurrency": concurrency,
        "duration_sec": duration,
        "max_tokens": MAX_TOKENS,
        "target_prompt_tokens": TARGET_PROMPT,
        "unique_tokens": UNIQUE_TOKENS,
        "local_base": LOCAL_BASE,
        "local_model": LOCAL_MODEL,
        "sf_base": SF_BASE,
        "sf_model": SF_MODEL,
        "note": "workload B shared-prefix + unique tail; thinking off; no :5180",
    }
    write_json(level_dir / "meta.json", meta)
    print(json.dumps({"event": "level_start", **meta}, ensure_ascii=False), flush=True)

    scrape_stop = threading.Event()
    scrape_th = None
    if target == "local":
        scrape_th = threading.Thread(target=scrape_loop, args=(metrics_path, scrape_stop), daemon=True)
        scrape_th.start()

    def worker(wid: int) -> None:
        nonlocal submitted, in_flight
        seq = 0
        while time.time() < stop_at:
            if budget is not None and not budget.ok():
                append_jsonl(events_path, {"event": "budget_stop", "spent": budget.spent}, lock)
                break
            seq += 1
            tag = f"w{wid}-s{seq}-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            content = shared + unique_block(tag)
            with lock:
                submitted += 1
                in_flight += 1
            rec = one_call(
                target=target,
                content=content,
                worker=wid,
                seq=seq,
                tag=tag,
                timeout=TIMEOUT,
                budget=budget,
            )
            with lock:
                in_flight -= 1
            append_jsonl(runs_path, rec, lock)

    t0 = time.time()
    stop_at = t0 + duration
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(worker, i) for i in range(concurrency)]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                append_jsonl(events_path, {"event": "worker_crash", "tb": traceback.format_exc()}, lock)
    wall = time.time() - t0
    scrape_stop.set()
    if scrape_th:
        scrape_th.join(timeout=8)

    rows = []
    if runs_path.exists():
        rows = [json.loads(x) for x in runs_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = [r for r in rows if r.get("status") == "ok"]
    lat = [float(r["elapsed_sec"]) for r in ok]
    pt = [float(r["prompt_tokens"]) for r in ok if r.get("prompt_tokens") is not None]
    ct = [float(r["completion_tokens"]) for r in ok if r.get("completion_tokens") is not None]
    cached = [float(r.get("cached_tokens") or 0) for r in ok]
    finished = {
        "finished_at": utc_now(),
        "target": target,
        "concurrency": concurrency,
        "wall_sec": round(wall, 3),
        "submitted": submitted,
        "done": len(ok),
        "failed": len(rows) - len(ok),
        "http_429": sum(1 for r in rows if r.get("http_status") == 429 or (r.get("retries_429") or 0) > 0),
        "qps": (len(ok) / wall) if wall else 0.0,
        "e2e_avg": mean(lat) if lat else None,
        "e2e_p50": pct(lat, 50),
        "e2e_p95": pct(lat, 95),
        "prompt_tokens_avg": mean(pt) if pt else None,
        "completion_tokens_avg": mean(ct) if ct else None,
        "cached_tokens_avg": mean(cached) if cached else None,
        "sf_cny_sum": sum(float(r.get("sf_cny") or 0) for r in ok),
        "sf_budget_spent": None if budget is None else budget.spent,
        "metrics": summarize_metrics(metrics_path),
    }
    write_json(level_dir / "finished.json", finished)
    print(json.dumps({"event": "level_done", **finished}, ensure_ascii=False), flush=True)
    return finished


def cmd_plan(_: argparse.Namespace) -> None:
    shared = TARGET_PROMPT - UNIQUE_TOKENS
    print(
        f"""
工作负载 B 对照（只出方案，不打流量）
================================
数据集：早上 B 同一套共享前缀 + 独特尾巴，长度先定 3 万
  目标 prompt {TARGET_PROMPT} / unique {UNIQUE_TOKENS} / shared {shared}
  可缓存比例 {shared / TARGET_PROMPT:.0%}（对齐早上 ~55%）
  预计 usage.prompt ≈ {TARGET_PROMPT * USAGE_OVERHEAD:.0f}（早上 19k→20878 的模板开销）
  thinking 关，max_tokens={MAX_TOKENS}，temperature=0，stream=false

两端：
  236   http://127.0.0.1:8500  模型 Qwen/Qwen3.6-35B-A3B-FP8
  硅基  https://api.siliconflow.cn/v1  模型 Qwen/Qwen3.6-35B-A3B
  不打 :5180，不改 :8500 启动参数

阶梯：并发 1 / 8 / 32 / 80，每档 10 分钟，档间冷却 60s
顺序：先 236 四档，再硅基；硅基建议先 1+8

硅基刹车：默认 SF_BUDGET_CNY={SF_BUDGET:.0f}，超了停
硅基开关：没有 --i-accept-sf-cost 不会出网

默认不跑。看完用 ./run.sh local 或 ./run.sh sf-align。
""".strip()
    )


def cmd_estimate(args: argparse.Namespace) -> None:
    duration = int(getattr(args, "duration", 600) or 600)
    prompt = TARGET_PROMPT * USAGE_OVERHEAD
    completion = MORNING["completion_tokens_avg"]
    per = sf_bill(prompt, completion)
    # 19k 早上经验 QPS，按 prompt 变长同比下调（prefill 变重）
    scale = 19000 / float(TARGET_PROMPT)
    guesses = {1: 0.33 * scale, 8: 1.00 * scale, 32: 2.00 * scale, 80: 2.50 * scale}
    print(f"硅基花费预估（prompt 目标 {TARGET_PROMPT}，usage 按早上 ×{USAGE_OVERHEAD:.2f}）")
    print(f"单条牌价：prompt {prompt:.0f} + completion {completion:.0f} → ¥{per['total_cny']:.4f}")
    print(f"每档 {duration}s，输入 ¥{SF_IN}/M 输出 ¥{SF_OUT}/M，缓存价={'同输入' if not SF_CACHE else SF_CACHE}")
    print(f"QPS 按 19k 经验 × {scale:.2f}（3 万更长，请求数往少估；若 QPS 不掉，花费更高）")
    print(f"{'conc':>6} {'qps~':>8} {'reqs~':>8} {'cny~':>8}")
    total_cny = 0.0
    total_n = 0
    for c, qps in guesses.items():
        n = int(qps * duration)
        cny = n * per["total_cny"]
        total_cny += cny
        total_n += n
        print(f"{c:6d} {qps:8.2f} {n:8d} {cny:8.1f}")
    print(f"{'sum':>6} {'':>8} {total_n:8d} {total_cny:8.1f}")
    align = (guesses[1] + guesses[8]) * duration * per["total_cny"]
    print(f"默认预算封顶 ¥{SF_BUDGET:.0f}。建议先 sf-align（1+8 ≈ ¥{align:.0f}）")
    print()
    qps30 = MORNING["qps"] * scale
    print(f"236 TCO 预览（早上 2.65 QPS × {scale:.2f} ≈ {qps30:.2f}，现网 DP4 要以新跑为准）")
    print(f"{'scenario':<28} {'cny/h':>8} {'cny/req':>8} {'sf/req':>8} {'236/sf':>8}")
    for name, capex, years, util, kw in TCO_SCENARIOS:
        h = tco_hourly(capex, years, util, kw)
        cny_req = tco_per_req(h["total_cny_h"], qps30)
        ratio = (cny_req / per["total_cny"]) if cny_req is not None else None
        print(
            f"{name:<28} {h['total_cny_h']:8.2f} {cny_req:8.4f} {per['total_cny']:8.4f} {ratio:8.2f}"
        )


def attach_tco(row: dict) -> dict:
    qps = float(row.get("qps") or 0) or 0.0
    wall_h = float(row.get("wall_sec") or 0) / 3600.0
    out = dict(row)
    for name, capex, years, util, kw in TCO_SCENARIOS:
        h = tco_hourly(capex, years, util, kw)
        out[f"{name}_cny_h"] = h["total_cny_h"]
        out[f"{name}_cny_req"] = tco_per_req(h["total_cny_h"], qps)
        out[f"{name}_cny_window"] = h["total_cny_h"] * wall_h
    power_w = (row.get("metrics") or {}).get("power_w_avg")
    if power_w:
        meas_h = (power_w / 1000.0) * YUAN_KWH
        mid = tco_hourly(*TCO_SCENARIOS[0][1:])
        out["measured_power_cny_h"] = meas_h
        out["mid_plus_measured_power_cny_h"] = mid["amort_cny_h"] + meas_h
    pt = float(row.get("prompt_tokens_avg") or 0)
    ct = float(row.get("completion_tokens_avg") or 0)
    cached = float(row.get("cached_tokens_avg") or 0)
    n = int(row.get("done") or 0)
    if row.get("target") == "sf":
        bill = sf_bill(pt * n, ct * n, cached * n)
        out["sf_cny_window_recalc"] = bill["total_cny"]
        out["sf_cny_req"] = (bill["total_cny"] / n) if n else None
    elif n and pt:
        # same tokens billed as if they went to SF
        bill = sf_bill(pt * n, ct * n, 0.0)
        out["sf_list_cny_if_same_tokens"] = bill["total_cny"]
        out["sf_list_cny_req_if_same_tokens"] = bill["total_cny"] / n
    return out


def load_finished(root: Path) -> list[dict]:
    rows = []
    for p in sorted(root.glob("*/finished.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["_path"] = str(p)
        rows.append(attach_tco(rec))
    return rows


def fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def print_report(rows: list[dict]) -> None:
    if not rows:
        print("没有 finished.json。先跑 ./run.sh local 或看 ./run.sh demo")
        return
    cols = [
        "target",
        "concurrency",
        "done",
        "failed",
        "qps",
        "e2e_p50",
        "e2e_p95",
        "prompt_tokens_avg",
        "completion_tokens_avg",
        "cached_tokens_avg",
    ]
    print("latency / tokens")
    print(" | ".join(cols))
    for r in rows:
        print(
            " | ".join(
                fmt(r.get(c), 3 if c in {"qps", "e2e_p50", "e2e_p95"} else 1)
                for c in cols
            )
        )
    print()
    print("cost  (mid=36万/3年/60%/2.0kW  dear=44万/2年/30%/2.4kW  cheap=36万/3年/90%/1.6kW)")
    print("target conc qps  sf¥/req  mid¥/req  dear¥/req  cheap¥/req  prefix_hit  computed_prefill")
    for r in rows:
        mets = r.get("metrics") or {}
        sf_req = r.get("sf_cny_req")
        if sf_req is None:
            sf_req = r.get("sf_list_cny_req_if_same_tokens")
        print(
            f"{r.get('target'):<6} {r.get('concurrency'):>3} {fmt(r.get('qps'),2):>6} "
            f"{fmt(sf_req,4):>8} {fmt(r.get('mid_36w_3y_60_2kW_cny_req'),4):>8} "
            f"{fmt(r.get('dear_idle_44w_2y_30_2k4_cny_req'),4):>8} "
            f"{fmt(r.get('cheap_full_36w_3y_90_1k6_cny_req'),4):>8} "
            f"{fmt(mets.get('prefix_hit'),3):>10} {fmt(mets.get('computed_prefill_delta'),0):>10}"
        )


def cmd_report(args: argparse.Namespace) -> None:
    if getattr(args, "demo_morning", False):
        demo = attach_tco(
            {
                "target": "local",
                "concurrency": 80,
                "wall_sec": MORNING["duration_sec"],
                "done": MORNING["done"],
                "failed": 0,
                "qps": MORNING["qps"],
                "e2e_p50": MORNING["e2e_p50"],
                "e2e_p95": None,
                "prompt_tokens_avg": MORNING["prompt_tokens_avg"],
                "completion_tokens_avg": MORNING["completion_tokens_avg"],
                "cached_tokens_avg": MORNING["prompt_tokens_avg"] * MORNING["prefix_hit"],
                "metrics": {
                    "prefix_hit": MORNING["prefix_hit"],
                    "computed_prefill_delta": None,
                    "note": "demo from morning TP4 80x20m B, not a new run",
                },
            }
        )
        print("DEMO：早上 TP4 80×20m B（19k，不是这次 3 万）套进 TCO 表，不是新压测")
        print_report([demo])
        return
    root = Path(args.dir)
    rows = load_finished(root)
    print_report(rows)
    write_json(root / "report.json", rows)
    print(f"wrote {root / 'report.json'}")


def cmd_run(args: argparse.Namespace) -> None:
    out_dir = Path(os.environ.get("VIKNOW_OUT_DIR", ".")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    concs = [int(x) for x in args.concurrency.split(",") if x.strip()]
    target = args.target
    if target == "sf" and not args.i_accept_sf_cost:
        raise SystemExit("refused: sf requires --i-accept-sf-cost")
    if target == "sf" and not SF_KEY:
        raise SystemExit("sf run refused: SILICONFLOW_API_KEY is empty")

    shared_path = out_dir / "shared_prefix.txt"
    if target == "local":
        print("calibrate shared prefix on :8500 /tokenize (not a load)", flush=True)
        shared, shared_tok = build_shared(shared_path if shared_path.exists() else None)
        shared_path.write_text(shared, encoding="utf-8")
        uniq = unique_block("calibrate")
        uniq_tok = tokenize_local(uniq)
        code, probe = http_json(
            LOCAL_BASE + "/v1/chat/completions",
            chat_body(LOCAL_MODEL, shared + uniq, 1),
            timeout=180,
        )
        probe_pt = (probe.get("usage") or {}).get("prompt_tokens") if code == 200 else None
        write_json(
            out_dir / "calibrate.json",
            {
                "shared_tokenize": shared_tok,
                "unique_tokenize": uniq_tok,
                "probe_http": code,
                "probe_prompt_tokens": probe_pt,
            },
        )
        print(
            json.dumps(
                {
                    "event": "calibrate",
                    "shared_tokenize": shared_tok,
                    "unique_tokenize": uniq_tok,
                    "probe_prompt_tokens": probe_pt,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    else:
        if not shared_path.exists():
            raise SystemExit(f"sf needs {shared_path} from a prior local calibrate/run")
        shared = shared_path.read_text(encoding="utf-8")

    summary = []
    for i, c in enumerate(concs):
        rec = run_level(
            out_dir=out_dir,
            target=target,
            concurrency=c,
            duration=args.duration,
            shared=shared,
            accept_sf=args.i_accept_sf_cost,
        )
        summary.append(rec)
        if i + 1 < len(concs) and target == "local" and COOLDOWN_SEC > 0:
            print(json.dumps({"event": "cooldown", "sec": COOLDOWN_SEC}), flush=True)
            time.sleep(COOLDOWN_SEC)
    write_json(out_dir / f"summary_{target}.json", summary)
    print_report([attach_tco(x) for x in summary])


def main() -> None:
    p = argparse.ArgumentParser(description="Workload B :8500 vs SiliconFlow (review/run)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("plan", help="print plan, no traffic")
    e = sub.add_parser("estimate", help="print SF spend + TCO preview, no traffic")
    e.add_argument("--duration", type=int, default=600)

    r = sub.add_parser("run", help="actually send traffic")
    r.add_argument("--target", choices=["local", "sf"], required=True)
    r.add_argument("--concurrency", default="1,8,32,80")
    r.add_argument("--duration", type=int, default=600)
    r.add_argument("--i-accept-sf-cost", action="store_true")

    rep = sub.add_parser("report", help="build table from out/*/finished.json")
    rep.add_argument("--dir", default=os.environ.get("VIKNOW_OUT_DIR", "."))
    rep.add_argument("--demo-morning", action="store_true")

    args = p.parse_args()
    _assert_tco_math()
    if args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "estimate":
        cmd_estimate(args)
    elif args.cmd == "run":
        cmd_run(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
