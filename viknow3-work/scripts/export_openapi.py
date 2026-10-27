#!/usr/bin/env python3
"""Export FastAPI OpenAPI schema for the docs site."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from viknow3.server import create_app  # noqa: E402

OUT = ROOT / "openapi" / "viknow3.json"

TAG_META = [
    {"name": "Users", "x-displayName": "用户", "description": "用户与账号"},
    {"name": "Agent", "x-displayName": "智能体", "description": "Agent 列表、Profile、模型与权限"},
    {"name": "Chat", "x-displayName": "对话", "description": "会话、轮次、SSE 事件与对话文件"},
    {"name": "Knowledge", "x-displayName": "知识库", "description": "知识库、索引检索与运维配置"},
]


def tag_for_path(path: str) -> str:
    """Map HTTP paths to the four product modules (sidebar groups)."""
    if path.startswith("/api/v1/users"):
        return "Users"
    if (
        path.startswith("/api/v1/knowledge")
        or path.startswith("/api/v1/om")
        or "/online/libraries" in path
    ):
        return "Knowledge"
    if path.startswith("/api/v1/agent"):
        chat_markers = (
            "/sessions",
            "/runs",
            "/files",
            "/workspace",
            "/events",
            "/history",
            "/interrupt",
        )
        if any(m in path for m in chat_markers):
            return "Chat"
        if path.startswith("/api/v1/agent/runs") or path.startswith("/api/v1/agent/sessions"):
            return "Chat"
        return "Agent"
    return "Knowledge"


def enrich_schema(schema: dict) -> dict:
    schema.setdefault("info", {})
    schema["info"].update(
        {
            "title": "ViKnow 3.0 API",
            "description": (
                "ViKnow 接入层 HTTP API。默认请求头：`Authorization: Bearer <app_key>`；"
                "开发环境可使用 `dev-app-key`。"
            ),
            "version": "3.0.0",
        }
    )
    schema["tags"] = TAG_META
    port = os.environ.get("VIKNOW_DOCS_PORT", os.environ.get("VIKNOW_PORT", "5197"))
    public_host = os.environ.get("VIKNOW_DOCS_PUBLIC_HOST", "36.103.198.236")
    schema["servers"] = [
        {
            "url": f"http://{public_host}:{port}",
            "description": "236 viknow3 接入层（demo 默认 VIKNOW_PORT=5197；5175 为 viknow2-test）",
        },
        {
            "url": f"http://127.0.0.1:{port}",
            "description": "本机接入层（与 deploy/demo/runtime.env 一致）",
        },
    ]
    for path, methods in schema.get("paths", {}).items():
        tag = tag_for_path(path)
        for method, op in methods.items():
            if method.startswith("x") or not isinstance(op, dict):
                continue
            op["tags"] = [tag]
            summary = op.get("summary") or op.get("operationId") or f"{method.upper()} {path}"
            op["summary"] = summary
    return schema


def main() -> None:
    app = create_app()
    schema = enrich_schema(app.openapi())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n = len(schema.get("paths", {})); print(f"Wrote {OUT} ({n} paths)")


if __name__ == "__main__":
    main()
