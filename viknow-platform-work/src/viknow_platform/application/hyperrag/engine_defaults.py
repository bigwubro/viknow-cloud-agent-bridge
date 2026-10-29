"""HyperRAG engine knobs not stored in viknow_ops (runtime constants only)."""

from __future__ import annotations

from typing import Any

# Mirrors viknow2 test hyperrag.yml component settings — not connection secrets.
ENGINE_DEFAULTS: dict[str, Any] = {
    "workspace": "default",
    "enable_task_management": True,
    "enable_rw_lock": False,
    "initial_mode": "read_write",
    "rerank_enabled": True,
    "rerank_mode": "text",
    "rerank_top_k": 50,
    "rerank_score_threshold": 0.5,
    "rerank_timeout_seconds": 15,
    "retrieval": {"vector_top_k": 30, "bm25_top_k": 60, "image_top_k": 3},
    "knowledgebase_summary_max_chars": 8000,
    "fast_ingest_enabled": True,
    "post_compile_auto_enabled": True,
    "post_compile_auto_interval_minutes": 15,
    "mineru_backend": "vlm-http-client",
    "bm25_enabled": True,
    "kg_vector_enabled": True,
    "max_doc_parse_workers": 2,
    "max_video_parse_workers": 2,
    "max_index_workers": 2,
    "ER_extraction_max_async": 8,
    "embedding_max_async": 16,
    "embedding_dim": 1024,
    "enable_video_embedding": True,
    "video_embedding_dim": 1024,
}
