"""Columns the config UI is allowed to change."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    type: str
    secret: bool = False
    nullable: bool = False
    choices: tuple[str, ...] = ()


TABLES: dict[str, dict[str, Column]] = {
    "model_provider": {
        "base_url": Column("text"),
        "model_name": Column("text", nullable=True),
        "api_key": Column("text", secret=True, nullable=True),
    },
    "model": {
        "provider_id": Column("text"),
        "name": Column("text"),
        "label": Column("text"),
    },
    "agent_model": {
        "model_id": Column("text"),
    },
    "postgres_connection": {
        "host": Column("text"),
        "port": Column("int"),
        "user": Column("text"),
        "password": Column("text", secret=True, nullable=True),
        "database": Column("text"),
    },
    "redis_connection": {
        "host": Column("text"),
        "port": Column("int"),
        "username": Column("text"),
        "database": Column("int"),
        "password": Column("text", secret=True, nullable=True),
    },
    "neo4j_connection": {
        "uri": Column("text"),
        "username": Column("text"),
        "password": Column("text", secret=True, nullable=True),
    },
    "s3_client": {
        "endpoint": Column("text"),
        "public_endpoint": Column("text"),
        "bucket": Column("text"),
        "region": Column("text"),
        "url_expires_seconds": Column("int"),
        "access_key": Column("text", secret=True, nullable=True),
        "secret_key": Column("text", secret=True, nullable=True),
    },
    "retrieval_setting": {
        "library": Column("text"),
        "mode": Column("text", choices=("hybrid", "vector", "bm25")),
        "top_k": Column("int"),
        "rerank_enabled": Column("bool"),
    },
    "index_setting": {
        "segmentation": Column("text", choices=("semantic", "fixed")),
        "asr_index": Column("text", choices=("qwen_vad", "qwen_diarized", "openai")),
        "asr_qa": Column("text", choices=("qwen_vad", "qwen_diarized", "openai")),
        "ocr": Column("text", choices=("paddleocr",)),
        "mineru_model": Column("text"),
    },
    "observe_setting": {
        "langfuse_enabled": Column("bool"),
        "langfuse_host": Column("text"),
        "langfuse_public_key": Column("text", secret=True, nullable=True),
        "langfuse_secret_key": Column("text", secret=True, nullable=True),
        "tracing_enabled": Column("bool"),
        "media_enabled": Column("bool"),
        "otel_enabled": Column("bool"),
        "otel_endpoint": Column("text"),
        "usage_enabled": Column("bool"),
        "usage_url": Column("text"),
        "usage_token": Column("text", secret=True, nullable=True),
    },
    "service_setting": {
        "log_level": Column("text", choices=("debug", "info", "warning")),
        "admin_username": Column("text"),
        "admin_password": Column("text", secret=True, nullable=True),
    },
}

SINGLETONS = {
    "postgres_connection",
    "redis_connection",
    "neo4j_connection",
    "s3_client",
    "retrieval_setting",
    "index_setting",
    "observe_setting",
    "service_setting",
}

ROW_KEY = {
    "model_provider": "id",
    "model": "id",
    "agent_model": "agent_id",
}
