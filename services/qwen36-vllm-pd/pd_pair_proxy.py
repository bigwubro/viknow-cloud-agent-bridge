#!/usr/bin/env python3
"""PD proxy: one or more prefills, one decode. Forwards P KV handshake to D.

Default prefill routing is least-inflight so the three P GPUs share load.
--route prefix_hash is optional: blake2b of the first --prefix-hash-chars
characters pins a shared head to one P cache.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("pd_pair_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="PD pair proxy")
P_BASES: list[str] = []
P_INFLIGHT: list[int] = []
P_PICKS: list[int] = []
P_LOCK = asyncio.Lock()
D_BASE = ""
MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"
CLIENT: httpx.AsyncClient | None = None
PREFIX_HASH_CHARS = 8192
ROUTE = "least_inflight"


@app.on_event("startup")
async def _startup() -> None:
    global CLIENT
    CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))


@app.on_event("shutdown")
async def _shutdown() -> None:
    if CLIENT:
        await CLIENT.aclose()


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
        return "".join(parts)
    return str(content)


def request_prefix_text(body: dict[str, Any]) -> str:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return "\n".join(
            _text_from_content(m.get("content")) for m in messages if isinstance(m, dict)
        )
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        return "".join(str(x) for x in prompt)
    if prompt is None:
        return ""
    return str(prompt)


def prefix_index(text: str, n_prefills: int, hash_chars: int) -> int:
    if n_prefills <= 0:
        raise ValueError("n_prefills must be positive")
    key = text[: max(hash_chars, 0)].encode("utf-8", errors="replace")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return int.from_bytes(digest, "big") % n_prefills


def _p_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["stream"] = False
    payload["max_tokens"] = 1
    payload.pop("max_completion_tokens", None)
    payload.pop("min_tokens", None)
    payload.pop("stream_options", None)
    payload["kv_transfer_params"] = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_engine_id": None,
        "remote_block_ids": None,
        "remote_host": None,
        "remote_port": None,
    }
    return payload


async def _acquire_prefill(body: dict[str, Any]) -> tuple[int, str]:
    async with P_LOCK:
        text = request_prefix_text(body)
        if ROUTE == "prefix_hash" and text:
            idx = prefix_index(text, len(P_BASES), PREFIX_HASH_CHARS)
        else:
            idx = min(range(len(P_INFLIGHT)), key=lambda i: P_INFLIGHT[i])
        P_INFLIGHT[idx] += 1
        P_PICKS[idx] += 1
        return idx, P_BASES[idx]


async def _release_prefill(idx: int) -> None:
    async with P_LOCK:
        P_INFLIGHT[idx] = max(0, P_INFLIGHT[idx] - 1)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "route": ROUTE,
        "prefix_hash_chars": PREFIX_HASH_CHARS,
        "prefills": list(P_BASES),
        "prefill": P_BASES[0] if P_BASES else "",
        "decode": D_BASE,
        "inflight": list(P_INFLIGHT),
        "picks": list(P_PICKS),
    }


@app.get("/v1/models")
async def models() -> Response:
    assert CLIENT is not None
    r = await CLIENT.get(f"{P_BASES[0]}/v1/models")
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/tokenize")
async def tokenize(request: Request) -> Response:
    assert CLIENT is not None
    r = await CLIENT.post(
        f"{P_BASES[0]}/tokenize",
        content=await request.body(),
        headers={"content-type": "application/json"},
    )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/v1/chat/completions")
@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    assert CLIENT is not None
    path = request.url.path
    body = await request.json()
    client_stream = bool(body.get("stream"))

    idx, p_base = await _acquire_prefill(body)
    try:
        prefill = await CLIENT.post(f"{p_base}{path}", json=_p_payload(body))
    finally:
        await _release_prefill(idx)
    if prefill.status_code >= 400:
        log.error("prefill %s %s %s", p_base, prefill.status_code, prefill.text[:400])
        return Response(content=prefill.content, status_code=prefill.status_code, media_type="application/json")
    try:
        prefill_json = prefill.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "prefill returned non-json"}, status_code=502)
    kv = prefill_json.get("kv_transfer_params") or {}
    if kv:
        kv = dict(kv)
        kv["remote_host"] = httpx.URL(p_base).host
        body["kv_transfer_params"] = kv
    else:
        log.warning("prefill %s response missing kv_transfer_params", p_base)

    if client_stream:
        req = CLIENT.build_request("POST", f"{D_BASE}{path}", json=body)
        dresp = await CLIENT.send(req, stream=True)
        return StreamingResponse(
            dresp.aiter_bytes(),
            status_code=dresp.status_code,
            media_type=dresp.headers.get("content-type", "text/event-stream"),
        )

    body["stream"] = False
    decode = await CLIENT.post(f"{D_BASE}{path}", json=body)
    return Response(content=decode.content, status_code=decode.status_code, media_type="application/json")


def main() -> None:
    global D_BASE, MODEL, PREFIX_HASH_CHARS, ROUTE
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument(
        "--prefill",
        action="append",
        dest="prefills",
        required=True,
        help="Prefill base URL. Repeat for 3P+1D, e.g. --prefill http://127.0.0.1:8510",
    )
    p.add_argument("--decode", required=True, help="http://127.0.0.1:8513")
    p.add_argument("--model", default=MODEL)
    p.add_argument(
        "--route",
        choices=("least_inflight", "prefix_hash"),
        default="least_inflight",
        help="How to pick a P. least_inflight spreads load; prefix_hash pins a shared head.",
    )
    p.add_argument(
        "--prefix-hash-chars",
        type=int,
        default=8192,
        help="Hash only this many leading characters so a unique tail does not move the request.",
    )
    args = p.parse_args()
    P_BASES.clear()
    P_BASES.extend(u.rstrip("/") for u in args.prefills)
    P_INFLIGHT.clear()
    P_INFLIGHT.extend([0] * len(P_BASES))
    P_PICKS.clear()
    P_PICKS.extend([0] * len(P_BASES))
    D_BASE = args.decode.rstrip("/")
    MODEL = args.model
    PREFIX_HASH_CHARS = max(int(args.prefix_hash_chars), 0)
    ROUTE = args.route
    log.info(
        "pair proxy %s -> P %s D %s route=%s hash_chars=%s",
        args.port,
        P_BASES,
        D_BASE,
        ROUTE,
        PREFIX_HASH_CHARS,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
