#!/usr/bin/env python3
"""One 1P+1D pair proxy. Forwards P KV handshake params to D."""
from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("pd_pair_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="1P+1D pair proxy")
P_BASE = ""
D_BASE = ""
MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"
CLIENT: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global CLIENT
    CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))


@app.on_event("shutdown")
async def _shutdown() -> None:
    if CLIENT:
        await CLIENT.aclose()


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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "prefill": P_BASE, "decode": D_BASE}


@app.get("/v1/models")
async def models() -> Response:
    assert CLIENT is not None
    r = await CLIENT.get(f"{P_BASE}/v1/models")
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/tokenize")
async def tokenize(request: Request) -> Response:
    assert CLIENT is not None
    r = await CLIENT.post(f"{P_BASE}/tokenize", content=await request.body(), headers={"content-type": "application/json"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/v1/chat/completions")
@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    assert CLIENT is not None
    path = request.url.path
    body = await request.json()
    client_stream = bool(body.get("stream"))

    prefill = await CLIENT.post(f"{P_BASE}{path}", json=_p_payload(body))
    if prefill.status_code >= 400:
        log.error("prefill %s %s", prefill.status_code, prefill.text[:400])
        return Response(content=prefill.content, status_code=prefill.status_code, media_type="application/json")
    try:
        prefill_json = prefill.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "prefill returned non-json"}, status_code=502)
    kv = prefill_json.get("kv_transfer_params") or {}
    if kv:
        kv = dict(kv)
        kv["remote_host"] = httpx.URL(P_BASE).host
        body["kv_transfer_params"] = kv
    else:
        log.warning("prefill response missing kv_transfer_params")

    if client_stream:
        req = CLIENT.build_request("POST", f"{D_BASE}{path}", json=body)
        dresp = await CLIENT.send(req, stream=True)
        return StreamingResponse(dresp.aiter_bytes(), status_code=dresp.status_code, media_type=dresp.headers.get("content-type", "text/event-stream"))

    body["stream"] = False
    decode = await CLIENT.post(f"{D_BASE}{path}", json=body)
    return Response(content=decode.content, status_code=decode.status_code, media_type="application/json")


def main() -> None:
    global P_BASE, D_BASE, MODEL
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--prefill", required=True, help="http://127.0.0.1:8510")
    p.add_argument("--decode", required=True, help="http://127.0.0.1:8511")
    p.add_argument("--model", default=MODEL)
    args = p.parse_args()
    P_BASE = args.prefill.rstrip("/")
    D_BASE = args.decode.rstrip("/")
    MODEL = args.model
    log.info("pair proxy %s -> P %s D %s", args.port, P_BASE, D_BASE)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
