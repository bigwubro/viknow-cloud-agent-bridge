#!/usr/bin/env python3
"""SiliconFlow feasibility probe: auth, headers, short-prompt concurrency.

Does not send the 30k workload-B dataset. Key comes from SILICONFLOW_API_KEY.
Never prints the key.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import mean

BASE = os.environ.get("SF_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
MODEL = os.environ.get("SF_MODEL", "Qwen/Qwen3.6-35B-A3B")
KEY = os.environ.get("SILICONFLOW_API_KEY", "")
OUT = os.environ.get("PROBE_OUT", "probe_sf.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def call(path: str, payload: dict | None = None, timeout: float = 60) -> dict:
    url = BASE + path if path.startswith("/") else path
    headers = {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            body = json.loads(raw) if raw else {}
            return {
                "ok": True,
                "http": r.status,
                "sec": round(time.time() - t0, 3),
                "headers": hdrs,
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return {
            "ok": False,
            "http": exc.code,
            "sec": round(time.time() - t0, 3),
            "headers": hdrs,
            "body": body,
        }
    except Exception as exc:
        return {"ok": False, "http": None, "sec": round(time.time() - t0, 3), "error": f"{type(exc).__name__}: {exc}"}


def interesting_headers(hdrs: dict) -> dict:
    out = {}
    for k, v in (hdrs or {}).items():
        lk = k.lower()
        if any(s in lk for s in ("rate", "limit", "retry", "remain", "quota", "trace", "request-id")):
            out[k] = v
    return out


def one_chat(tag: str, max_tokens: int = 16) -> dict:
    rec = call(
        "/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": f"只回复一个字：好。probe={tag}"}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
            "enable_thinking": False,
        },
        timeout=90,
    )
    usage = (rec.get("body") or {}).get("usage") or {}
    return {
        "tag": tag,
        "ok": rec.get("ok"),
        "http": rec.get("http"),
        "sec": rec.get("sec"),
        "error": rec.get("error"),
        "message": (rec.get("body") or {}).get("message") or (rec.get("body") or {}).get("error"),
        "usage": usage,
        "finish": ((rec.get("body") or {}).get("choices") or [{}])[0].get("finish_reason"),
        "rate_headers": interesting_headers(rec.get("headers") or {}),
    }


def burst(concurrency: int, n: int) -> dict:
    rows = []
    lock = threading.Lock()
    t0 = time.time()

    def worker(i: int) -> None:
        rec = one_chat(f"c{concurrency}-{i}")
        with lock:
            rows.append(rec)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(worker, i) for i in range(n)]
        for fut in as_completed(futs):
            fut.result()
    wall = time.time() - t0
    ok = [r for r in rows if r.get("ok")]
    http_counts: dict[str, int] = {}
    for r in rows:
        key = str(r.get("http"))
        http_counts[key] = http_counts.get(key, 0) + 1
    lats = [r["sec"] for r in ok]
    prompt = sum(int((r.get("usage") or {}).get("prompt_tokens") or 0) for r in ok)
    completion = sum(int((r.get("usage") or {}).get("completion_tokens") or 0) for r in ok)
    return {
        "concurrency": concurrency,
        "n": n,
        "wall_sec": round(wall, 3),
        "ok": len(ok),
        "fail": len(rows) - len(ok),
        "http_counts": http_counts,
        "qps": round(len(ok) / wall, 3) if wall else 0,
        "gen_tps": round(completion / wall, 3) if wall else 0,
        "prompt_tokens_sum": prompt,
        "completion_tokens_sum": completion,
        "e2e_avg": round(mean(lats), 3) if lats else None,
        "e2e_p50": round(sorted(lats)[len(lats) // 2], 3) if lats else None,
        "sample_error": next((r.get("message") or r.get("error") for r in rows if not r.get("ok")), None),
        "sample_rate_headers": next((r.get("rate_headers") for r in rows if r.get("rate_headers")), {}),
    }


def main() -> None:
    if not KEY:
        raise SystemExit("SILICONFLOW_API_KEY empty")
    report: dict = {
        "started_at": utc_now(),
        "base": BASE,
        "model": MODEL,
        "note": "short-prompt probe only; not workload B 30k",
    }
    for path in ("/user/info", "/models"):
        rec = call(path)
        body = rec.get("body") or {}
        if path == "/models":
            ids = [x.get("id") for x in (body.get("data") or [])]
            report["models_http"] = rec.get("http")
            report["model_present"] = MODEL in ids
            report["qwen36_ids"] = [i for i in ids if i and "Qwen3.6" in i]
        else:
            safe = {}
            if isinstance(body, dict):
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                for k, v in (data or {}).items():
                    if any(s in k.lower() for s in ("balance", "charge", "status", "name", "id", "level", "rate", "limit", "tier")):
                        safe[k] = v
            report["user_info_http"] = rec.get("http")
            report["user_info"] = safe or {"keys": list(body)[:12] if isinstance(body, dict) else type(body).__name__}
            report["user_rate_headers"] = interesting_headers(rec.get("headers") or {})

    report["single"] = one_chat("single")
    # Small then wider: enough to see 429, cheap tokens.
    report["burst_c1_n8"] = burst(1, 8)
    report["burst_c8_n24"] = burst(8, 24)
    report["burst_c32_n32"] = burst(32, 32)
    report["finished_at"] = utc_now()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
