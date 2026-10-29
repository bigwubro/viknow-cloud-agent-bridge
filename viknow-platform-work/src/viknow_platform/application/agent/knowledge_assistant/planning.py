"""Retrieval planning via the configured chat model (not mock fixtures)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from viknow_platform.ops.config.model_resolve import resolve_openai_chat

_EXPERT_PLANNING_PREFIX = (
    "You are the ViKnow retrieval planning expert. "
    "Stay within the granted library_id, prefer evidence-first QA, and output strict JSON only.\n"
)


def _chat_json(system: str, user: str) -> dict[str, Any]:
    resolved = resolve_openai_chat("server-default")
    payload = {
        "model": resolved.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {resolved.api_key}"}
    with httpx.Client(base_url=resolved.base_url.rstrip("/"), timeout=120.0) as client:
        response = client.post("/chat/completions", json=payload, headers=headers)
    response.raise_for_status()
    content = str((response.json().get("choices") or [{}])[0].get("message", {}).get("content") or "")
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, flags=re.S)
    text = fence.group(1) if fence else content.strip()
    return json.loads(text)


def refine_queries(query: str, library_id: str, *, max_queries: int = 3) -> dict[str, Any]:
    data = _chat_json(
        _EXPERT_PLANNING_PREFIX
        + "Return JSON only: {\"retrieval_queries\":[\"...\"],\"answer_slots\":[{\"slot_id\":\"main\",\"question\":\"...\"}]}. "
        f"At most {max_queries} distinct search queries for one library.",
        f"library_id={library_id}\nuser_query={query}",
    )
    queries = [str(item).strip() for item in data.get("retrieval_queries") or [] if str(item).strip()]
    if not queries:
        queries = [query.strip()]
    slots = data.get("answer_slots")
    if not isinstance(slots, list) or not slots:
        slots = [{"slot_id": "main", "question": query.strip()}]
    return {
        "library_id": library_id,
        "retrieval_queries": queries[:max_queries],
        "answer_slots": slots,
        "source": "llm",
    }


def answer_plan(query: str, library_id: str) -> dict[str, Any]:
    data = _chat_json(
        _EXPERT_PLANNING_PREFIX
        + "Return JSON only: {\"slots\":[{\"slot_id\":\"main\",\"question\":\"...\",\"status\":\"open\"}],"
        "\"subquestions\":[\"...\"]}.",
        f"library_id={library_id}\nuser_query={query}",
    )
    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        slots = [{"slot_id": "main", "question": query, "status": "open"}]
    subquestions = [str(item).strip() for item in data.get("subquestions") or [] if str(item).strip()]
    if not subquestions:
        subquestions = [query]
    return {
        "library_id": library_id,
        "query": query,
        "slots": slots,
        "subquestions": subquestions,
        "source": "llm",
    }
