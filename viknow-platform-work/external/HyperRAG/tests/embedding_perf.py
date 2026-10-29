"""
Embedding API 性能测试脚本。

默认使用 OpenAI 兼容接口调用 http://dev.ai.qingxiang.tech:8080/v1 ，
可通过参数覆盖 base_url、api_key、模型、批大小和并发度。
"""

from __future__ import annotations

import argparse
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil, floor
from statistics import mean
from typing import Iterable, List

from openai import OpenAI

DEFAULT_BATCH_SIZES = [16, 24, 32]
DEFAULT_CONCURRENCY = [4, 5, 6, 7, 8, 9, 10, 11, 12]
DEFAULT_REQUESTS = 100
DEFAULT_BASE_URL = "http://dev.ai.qingxiang.tech:8080/v1"
DEFAULT_API_KEY = os.getenv("HYPERRAG_TEST_LLM_API_KEY", "QX123456")
DEFAULT_EMBED_MODEL = os.getenv("HYPERRAG_TEST_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")

CN_FRAGMENTS = [
    "人工智能",
    "大模型",
    "检索增强",
    "知识库",
    "视频理解",
    "文本嵌入",
    "并发请求",
    "性能测试",
    "数据处理",
    "推理优化",
]

EN_FRAGMENTS = [
    "retrieval",
    "embedding",
    "performance",
    "multimodal",
    "context",
    "throughput",
    "latency",
    "vector",
    "pipeline",
    "evaluation",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embedding API 性能测试")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI 兼容接口 base url")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="接口 API Key")
    parser.add_argument("--model", default=DEFAULT_EMBED_MODEL, help="Embedding 模型名称")
    parser.add_argument(
        "--batch-sizes",
        type=int,
        nargs="+",
        default=DEFAULT_BATCH_SIZES,
        help="每次请求的文本条数",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        nargs="+",
        default=DEFAULT_CONCURRENCY,
        help="线程并发数",
    )
    parser.add_argument(
        "--requests-per-setting",
        type=int,
        default=DEFAULT_REQUESTS,
        help="每个批大小与并发组合下的请求次数",
    )
    parser.add_argument(
        "--warmup-requests",
        type=int,
        default=20,
        help="每个批大小与并发组合下的预热请求次数（不计入统计）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子，便于复现",
    )
    return parser.parse_args()


def ensure_v1_suffix(base_url: str) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def random_text() -> str:
    total_fragments = random.randint(12, 32)
    fragments: List[str] = []
    for _ in range(total_fragments):
        if random.random() < 0.5:
            fragments.append(random.choice(EN_FRAGMENTS))
        else:
            fragments.append(random.choice(CN_FRAGMENTS))
    return " ".join(fragments)


def percentile(values: Iterable[float], p: float) -> float:
    """线性插值分位数，p ∈ [0, 1]。

    说明：原实现用 int(n*p)-1 会在 n=100,p=0.95 时“吃掉”最慢 5 个样本，
    容易导致 p95 过小，掩盖长尾。
    """
    seq = sorted(values)
    if not seq:
        return 0.0
    p = max(0.0, min(1.0, p))
    if len(seq) == 1:
        return seq[0]
    pos = (len(seq) - 1) * p
    lo = int(floor(pos))
    hi = int(ceil(pos))
    if lo == hi:
        return seq[lo]
    weight = pos - lo
    return seq[lo] * (1.0 - weight) + seq[hi] * weight


def run_once(client: OpenAI, model: str, texts: list[str]) -> float:
    start = time.perf_counter()
    client.embeddings.create(model=model, input=texts)
    return time.perf_counter() - start


def benchmark(
    client: OpenAI,
    model: str,
    batch_size: int,
    concurrency: int,
    requests_per_setting: int,
    warmup_requests: int,
) -> tuple[list[float], int, float]:
    durations: list[float] = []
    failures = 0

    # 预生成输入，避免把本地随机/拼接开销混进服务端延迟里
    warmup_inputs = [[random_text() for _ in range(batch_size)] for _ in range(max(0, warmup_requests))]
    measured_inputs = [[random_text() for _ in range(batch_size)] for _ in range(requests_per_setting)]

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        # warmup phase（不计入统计）
        warmup_futures = [executor.submit(run_once, client, model, texts) for texts in warmup_inputs]
        for fut in as_completed(warmup_futures):
            try:
                fut.result()
            except Exception:
                # 预热阶段失败也记录下来，便于发现服务端抖动/限流
                failures += 1

        # measured phase
        overall_start = time.perf_counter()
        futures = [executor.submit(run_once, client, model, texts) for texts in measured_inputs]
        for fut in as_completed(futures):
            try:
                durations.append(fut.result())
            except Exception:
                failures += 1

    total_elapsed = time.perf_counter() - overall_start
    return durations, failures, total_elapsed


def main() -> None:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    client = OpenAI(base_url=ensure_v1_suffix(args.base_url), api_key=args.api_key)

    print(
        f"Base URL: {client.base_url} | Model: {args.model} | "
        f"Batch sizes: {args.batch_sizes} | Concurrency: {args.concurrency} | "
        f"Requests/setting: {args.requests_per_setting} | Warmup/setting: {args.warmup_requests}"
    )

    for batch_size in args.batch_sizes:
        for conc in args.concurrency:
            durations, failures, total = benchmark(
                client,
                args.model,
                batch_size,
                conc,
                args.requests_per_setting,
                args.warmup_requests,
            )
            success = len(durations)
            total_requests = success + failures
            per_request = mean(durations) if durations else 0.0
            p50 = percentile(durations, 0.50) if durations else 0.0
            p90 = percentile(durations, 0.90) if durations else 0.0
            p95 = percentile(durations, 0.95) if durations else 0.0
            p99 = percentile(durations, 0.99) if durations else 0.0
            mx = max(durations) if durations else 0.0
            throughput_reqs = total_requests / total if total > 0 else 0.0
            throughput_inputs = (total_requests * batch_size) / total if total > 0 else 0.0

            print(
                f"[batch={batch_size:02d} | concurrency={conc:02d}] "
                f"total={total_requests} success={success} fail={failures} "
                f"avg={per_request:.3f}s p50={p50:.3f}s p90={p90:.3f}s p95={p95:.3f}s p99={p99:.3f}s max={mx:.3f}s "
                f"req/s={throughput_reqs:.2f} input/s={throughput_inputs:.2f} "
                f"wall={total:.3f}s"
            )


if __name__ == "__main__":
    main()


