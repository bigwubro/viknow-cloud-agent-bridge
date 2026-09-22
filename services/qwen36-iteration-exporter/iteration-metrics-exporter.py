#!/usr/bin/env python3
"""Tail vllm-vlm docker logs and expose batch prefill/decode metrics for Prometheus."""
from __future__ import annotations

import argparse
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ITER_RE = re.compile(
    r"Iteration\((?P<num>\d+)\):\s*"
    r"(?P<ctx_req>\d+)\s+context requests,\s*"
    r"(?P<ctx_tok>\d+)\s+context tokens,\s*"
    r"(?P<gen_req>\d+)\s+generation requests,\s*"
    r"(?P<gen_tok>\d+)\s+generation tokens,\s*"
    r"iteration elapsed time:\s*"
    r"(?P<elapsed>[0-9.]+)\s*ms"
    # vLLM 0.26 logs GPU KV cache usage between elapsed time and encoder fields.
    r"(?:,\s*GPU KV cache usage:\s*[0-9.]+%)?"
    r"(?:,\s*encoder inputs:\s*(?P<enc_in>\d+),\s*"
    r"encoder output embeddings:\s*(?P<enc_emb>\d+))?"
)


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_scrape_ts = 0.0
        self.iteration = 0
        self.context_requests = 0
        self.context_tokens = 0
        self.generation_requests = 0
        self.generation_tokens = 0
        self.encoder_inputs = 0
        self.encoder_output_embeddings = 0
        self.elapsed_ms = 0.0
        self.iterations_total = 0
        self.context_tokens_total = 0
        self.generation_tokens_total = 0
        self.lines_seen = 0
        self.lines_matched = 0
        self.last_line_ts = 0.0
        self.log_reader_alive = 1

    def observe(self, m: re.Match[str]) -> None:
        with self.lock:
            self.iteration = int(m.group("num"))
            self.context_requests = int(m.group("ctx_req"))
            self.context_tokens = int(m.group("ctx_tok"))
            self.generation_requests = int(m.group("gen_req"))
            self.generation_tokens = int(m.group("gen_tok"))
            enc_in = m.group("enc_in")
            enc_emb = m.group("enc_emb")
            self.encoder_inputs = int(enc_in) if enc_in else 0
            self.encoder_output_embeddings = int(enc_emb) if enc_emb else 0
            self.elapsed_ms = float(m.group("elapsed"))
            self.iterations_total += 1
            self.context_tokens_total += self.context_tokens
            self.generation_tokens_total += self.generation_tokens
            self.lines_matched += 1
            self.last_line_ts = time.time()

    def render(self) -> str:
        with self.lock:
            total_tok = self.context_tokens + self.generation_tokens
            prefill_ratio = (
                self.context_tokens / total_tok if total_tok > 0 else 0.0
            )
            return "\n".join(
                [
                    "# HELP vllm_batch_iteration Current engine iteration number",
                    "# TYPE vllm_batch_iteration gauge",
                    f"vllm_batch_iteration {self.iteration}",
                    "# HELP vllm_batch_context_requests Context (prefill) requests in last iteration",
                    "# TYPE vllm_batch_context_requests gauge",
                    f"vllm_batch_context_requests {self.context_requests}",
                    "# HELP vllm_batch_context_tokens Context (prefill) tokens in last iteration",
                    "# TYPE vllm_batch_context_tokens gauge",
                    f"vllm_batch_context_tokens {self.context_tokens}",
                    "# HELP vllm_batch_generation_requests Generation (decode) requests in last iteration",
                    "# TYPE vllm_batch_generation_requests gauge",
                    f"vllm_batch_generation_requests {self.generation_requests}",
                    "# HELP vllm_batch_encoder_inputs MM encoder inputs scheduled in last iteration",
                    "# TYPE vllm_batch_encoder_inputs gauge",
                    f"vllm_batch_encoder_inputs {self.encoder_inputs}",
                    "# HELP vllm_batch_encoder_output_embeddings MM encoder output embeddings in last iteration",
                    "# TYPE vllm_batch_encoder_output_embeddings gauge",
                    f"vllm_batch_encoder_output_embeddings {self.encoder_output_embeddings}",
                    "# HELP vllm_batch_generation_tokens Generation (decode) tokens in last iteration",
                    "# TYPE vllm_batch_generation_tokens gauge",
                    f"vllm_batch_generation_tokens {self.generation_tokens}",
                    "# HELP vllm_batch_total_tokens Total tokens in last iteration",
                    "# TYPE vllm_batch_total_tokens gauge",
                    f"vllm_batch_total_tokens {total_tok}",
                    "# HELP vllm_batch_prefill_token_ratio Prefill token share in last iteration",
                    "# TYPE vllm_batch_prefill_token_ratio gauge",
                    f"vllm_batch_prefill_token_ratio {prefill_ratio:.6f}",
                    "# HELP vllm_batch_iteration_elapsed_ms Iteration elapsed time in milliseconds",
                    "# TYPE vllm_batch_iteration_elapsed_ms gauge",
                    f"vllm_batch_iteration_elapsed_ms {self.elapsed_ms}",
                    "# HELP vllm_batch_iterations_total Parsed iteration log lines",
                    "# TYPE vllm_batch_iterations_total counter",
                    f"vllm_batch_iterations_total {self.iterations_total}",
                    "# HELP vllm_batch_context_tokens_total Cumulative context tokens observed",
                    "# TYPE vllm_batch_context_tokens_total counter",
                    f"vllm_batch_context_tokens_total {self.context_tokens_total}",
                    "# HELP vllm_batch_generation_tokens_total Cumulative generation tokens observed",
                    "# TYPE vllm_batch_generation_tokens_total counter",
                    f"vllm_batch_generation_tokens_total {self.generation_tokens_total}",
                    "# HELP vllm_batch_exporter_lines_seen Docker log lines read",
                    "# TYPE vllm_batch_exporter_lines_seen counter",
                    f"vllm_batch_exporter_lines_seen {self.lines_seen}",
                    "# HELP vllm_batch_exporter_lines_matched Parsed iteration lines",
                    "# TYPE vllm_batch_exporter_lines_matched counter",
                    f"vllm_batch_exporter_lines_matched {self.lines_matched}",
                    "# HELP vllm_batch_exporter_log_reader_alive 1 while docker logs tail is running",
                    "# TYPE vllm_batch_exporter_log_reader_alive gauge",
                    f"vllm_batch_exporter_log_reader_alive {self.log_reader_alive}",
                    "# HELP vllm_batch_exporter_last_line_unixtime Unix time of last matched iteration line",
                    "# TYPE vllm_batch_exporter_last_line_unixtime gauge",
                    f"vllm_batch_exporter_last_line_unixtime {self.last_line_ts:.3f}",
                    "",
                ]
            )


def tail_docker_logs(container: str, metrics: Metrics) -> None:
    while True:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "0", container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            metrics.log_reader_alive = 1
            for line in proc.stdout:
                metrics.lines_seen += 1
                if "(dummy)" in line:
                    continue
                m = ITER_RE.search(line)
                if m:
                    metrics.observe(m)
        except Exception:
            metrics.log_reader_alive = 0
        finally:
            proc.kill()
            proc.wait()
        metrics.log_reader_alive = 0
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--container", default="vllm-vlm")
    args = parser.parse_args()

    metrics = Metrics()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/metrics", "/"):
                self.send_error(404)
                return
            body = metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    t = threading.Thread(
        target=tail_docker_logs, args=(args.container, metrics), daemon=True
    )
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"iteration metrics exporter on {args.host}:{args.port} (container={args.container})")
    server.serve_forever()


if __name__ == "__main__":
    main()
