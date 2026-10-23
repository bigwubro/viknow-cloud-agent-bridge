"""
配置加载工具

提供HyperRAG配置类和从YAML文件加载配置的功能
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 默认模型超时
DEFAULT_LLM_TIMEOUT = 120
DEFAULT_EMBEDDING_TIMEOUT = 60
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class EnvironmentInterpolationError(ValueError):
    """Raised when a config references an environment variable that is absent."""


def _interpolate_environment(value: Any) -> Any:
    """Resolve ``${NAME}`` values without ever including secret values in errors."""
    missing: set[str] = set()

    def resolve_string(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                missing.add(name)
                return match.group(0)
            return resolved

        return _ENV_REFERENCE.sub(replace, text)

    def resolve(item: Any) -> Any:
        if isinstance(item, str):
            return resolve_string(item)
        if isinstance(item, list):
            return [resolve(entry) for entry in item]
        if isinstance(item, dict):
            return {key: resolve(entry) for key, entry in item.items()}
        return item

    resolved_value = resolve(value)
    if missing:
        raise EnvironmentInterpolationError(
            "missing required environment variables: " + ", ".join(sorted(missing))
        )
    return resolved_value


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


@dataclass
class TaskLimitConfig:
    """单个任务类型的限制配置"""

    max_concurrent: int = 1
    gpu_memory_mb: int = 0


@dataclass
class UnifiedSchedulerConfig:
    """统一调度（scheduler-core）接入配置。

    开态时（enabled=True）HyperRAG 的 job/task 提交与 worker 取活走
    scheduler-core 队列；关闭时保持原 HyperRAG 调度逻辑不变。
    """

    enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    lease_ttl_seconds: int = 60
    heartbeat_interval_seconds: int = 15
    queue_wait_timeout_seconds: float = 30.0
    default_execution_timeout_seconds: float = 180.0
    # 资源域 -> 容量（缺省使用 scheduler-core 默认域）
    domains: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class HyperRAGConfig:
    """HyperRAG配置"""

    # 模型与接口配置
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: Optional[str] = None
    llm_model_kwargs: Dict[str, Any] = field(default_factory=dict)
    embedding_model: str = "text-embedding-3-small"
    embedding_provider: str = "vllm"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: Optional[str] = None
    embedding_dim: int = 1536
    embedding_max_async: int = 32
    embedding_params: Dict[str, Any] = field(default_factory=dict)
    llm_timeout: int = DEFAULT_LLM_TIMEOUT
    embedding_timeout: int = DEFAULT_EMBEDDING_TIMEOUT
    ER_extraction_max_async: int = 8
    max_parallel_insert: int = 10
    max_gleaning: int = 1
    enable_llm_cache: bool = True
    summary_max_tokens: int = 500
    summary_context_size: int = 4096
    summary_length_recommended: int = 600
    knowledgebase_summary_max_chars: int = 8000
    knowledge_discover_asset_matches_per_library: int = 3
    knowledge_discover_asset_match_library_limit: int = 20
    knowledge_discover_catalog_query_concurrency: int = 4
    max_graph_nodes: Optional[int] = None
    cosine_better_than_threshold: float = 0.2
    addon_params: Dict[str, Any] = field(default_factory=dict)

    # 索引配置
    working_dir: str = "./hyperrag-storage"
    workspace: str = "default"
    chunk_token_size: int = 512
    chunk_overlap_token_size: int = 0
    respect_semantic_boundaries: bool = True
    merge_small_tables_threshold_tokens: Optional[int] = 128
    enable_modal_replay: bool = False
    entity_types: Optional[List[str]] = None

    # 存储后端配置
    asset_status_storage_backend: str = "postgres"  # "json" | "postgres"

    # MinerU配置
    mineru_method: str = "auto"
    mineru_lang: Optional[str] = None
    mineru_device: Optional[str] = None
    mineru_backend: Optional[str] = "vlm-http-client"  # "pipeline" | "vlm-http-client"
    mineru_mid_path: str = "vlm" if mineru_backend == "vlm-http-client" else "auto"
    mineru_formula: Optional[bool] = None
    mineru_table: Optional[bool] = None
    mineru_cache_dir: Optional[str] = None  # 可选：外部注入 mineru 缓存目录（测试会传入）
    mineru_timeout_seconds: int = 3600  # mineru 子进程超时；超时后杀进程组，避免无限卡住 API
    # whisper
    whisper_api_key: str = "QX123456"
    whisper_base_url: str = "http://dev.ai.qingxiang.tech:8113/v1"

    # 多模态模型函数（拆分）
    vision_model_func: Optional[Callable] = None  # 图像/表格 caption 与 OCR 要点
    vision_model_max_concurrent: int = 4  # VLM并发请求数
    video_caption_func: Optional[Callable] = None  # 视频片段 caption

    # 视频处理配置
    video_enable_scene_detect: bool = False
    video_cache_dir: Optional[str] = None
    video_segment_length_sec: int = 30
    video_clip_sample_fps: float = 1.0
    video_asr_max_concurrent: int = 8
    video_post_process_hook: Optional[Callable] = None

    video_segment_output_format: str = "mp4"

    # Actio 视频解析配置由嵌入应用显式注入；HyperRAG 不提供包内回退路径。
    actio_models_config_path: Optional[str] = None
    actio_segment_config_path: Optional[str] = None
    actio_aggregate_config_path: Optional[str] = None
    actio_merge_config_path: Optional[str] = None
    actio_merge_dtw_config_path: Optional[str] = None
    actio_sop_schema_path: Optional[str] = None
    actio_device: str = "cuda:1"  # Actio 视频分割使用的设备
    actio_asr_max_concurrency: int = 8
    actio_annotate_max_concurrency: int = 8
    actio_keep_asr_segment_outputs: bool = False
    actio_keep_asr_audio_outputs: bool = True

    # 处理配置 - 三个独立任务池的并发数
    max_doc_parse_workers: int = 2  # 文档解析并发数
    max_video_parse_workers: int = 1  # 视频解析并发数（通常较慢，建议1-2个）
    max_index_workers: int = 4  # KG 入库并发数
    max_concurrent_job_tasks: int = 2  # 无 YAML 时兜底；生产由 app.yaml scheduler.unified 注入
    enable_indexing: bool = True  # 是否执行 KG 索引。测试时可关闭以加速

    # Contextual Retrieval 配置（Anthropic 提出的上下文嵌入技术）
    enable_contextual_embeddings: bool = True  # 是否启用上下文嵌入
    contextual_llm_func: Optional[Callable] = None  # 用于生成上下文的 LLM 函数
    contextual_max_tokens: int = 100  # 生成的上下文最大 token 数
    contextual_window_chunks: int = 2  # 上下文窗口大小（前后各取多少个相邻chunk）

    # 存储配置
    kv_storage: str = "PGKVStorage"
    vector_storage: str = "PGVectorStorage"
    graph_storage: str = "Neo4JStorage"
    doc_status_storage: str = "PGDocStatusStorage"

    # LLM配置
    llm_model_func: Optional[Callable] = None
    embed_model_func: Optional[Callable] = None

    # 进度回调配置
    index_progress_callback: Optional[Callable] = (
        None  # 索引进度回调 (kb_id, doc_id, progress, description)
    )

    # 任务管理配置
    enable_task_management: bool = False
    # 是否启用 RW 分布式锁（默认关闭：调试更友好；多节点部署时再打开）
    enable_rw_lock: bool = False
    redis_uri: str = "redis://localhost:6379/0"
    instance_id: str = "auto"
    initial_mode: str = "read_only"
    heartbeat_interval: int = 5
    failover_timeout: int = 30
    task_limits: Optional[Dict[str, TaskLimitConfig]] = None
    callbacks: Optional[Dict[str, Callable]] = None
    # 统一调度（scheduler-core）接入配置；None 或 enabled=False 时保持原调度逻辑
    scheduler_unified: Optional[UnifiedSchedulerConfig] = None

    # BM25配置
    bm25_enabled: bool = False
    bm25_mode: str = "simple"  # simple / zhparser, 当前只支持自定义预处理chunks + simple模式, 后续可考虑增加 zhparser 配置
    bm25_top_k: int = 60
    hybrid_vector_weight: float = 0.6
    hybrid_bm25_weight: float = 0.4
    bm25_rrf_k: int = 30
    bm25_stopwords_file: Optional[str] = None  # jieba 停用词文件路径
    bm25_custom_dict_file: Optional[str] = None  # jieba 用户词典文件路径
    bm25_zhparser_dict_file: Optional[str] = None  # zhparser 自定义词典文件路径
    bm25_zhparser_dict_table: Optional[str] = None  # zhparser 自定义词典数据库表名
    bm25_jieba_hmm: bool = True  # 是否启用 HMM 新词发现
    bm25_storage_backend: str = "postgres"  # "json" | "postgres"
    hybrid_top_k: int = 150  # RRF 融合后最终返回数量

    # 外部应用配置映射
    application_config_path: Optional[str] = None

    # KG 向量库配置
    kg_vector_enabled: bool = True
    kg_vector_schema: str = "hyperrag_kg"
    kg_vector_chunk_table: str = "kg_chunk_vectors"
    kg_vector_entity_table: str = "kg_entity_vectors"
    kg_vector_relation_table: str = "kg_relation_vectors"
    kg_vector_query_top_k: int = 24
    kg_vector_entity_top_k: int = 20
    kg_vector_relation_top_k: int = 24
    kg_vector_chunk_top_k: int = 24
    vector_retrieval_top_k: int = 24
    image_retrieval_top_k: int = 3
    kg_vector_embedding_batch_size: int = 16
    # 视频 KG 实验开关：直接从结构化视频语义中提取实体关系
    use_structured_video_er_extraction: bool = False

    # 查询期 synonym 扩展配置
    query_synonym_expand_enabled: bool = True
    query_synonym_expand_limit: int = 8
    query_synonym_similarity_threshold: float = 0.82

    # Fast ingest / VKR / post-compile / lint
    fast_ingest_enabled: bool = False
    fast_ingest_compile_phase: str = "FAST_INGEST"
    fast_ingest_model: Optional[str] = None
    fast_ingest_provider: Optional[str] = None
    fast_ingest_base_url: Optional[str] = None
    fast_ingest_api_key: Optional[str] = None
    fast_ingest_decoding_strategy: str = "single_model"
    fast_ingest_er_prompt_mode: str = "auto"  # auto | full | compact
    fast_ingest_er_max_output_tokens: int = 512
    fast_ingest_er_description_max_chars: int = 64
    fast_ingest_er_retry_full_on_empty: bool = False
    precompile_enabled: bool = False
    precompile_workspace: str = "PRECOMPILED"
    precompile_refine_enabled: bool = True
    precompile_refine_top_k: int = 8
    precompile_refine_threshold: float = 0.0
    vkr_enabled: bool = False
    vkr_delta_top_k: int = 8
    post_compile_auto_enabled: bool = True
    post_compile_auto_interval_minutes: int = 15
    post_compile_hot_buffer_edge_threshold: int = 5000
    post_compile_max_batches_per_cycle: int = 2
    post_compile_batch_size: int = 5000
    lint_batch_size: int = 100
    lint_auto_start_hour: int = 0
    lint_auto_end_hour: int = 24

    # 查询期 rerank 配置（请求参数可覆盖全局默认）
    rerank_enabled: bool = True
    rerank_base_url: str = "http://ai.qingxiang.tech:8602/v1"
    rerank_model: str = "Qwen/Qwen3-VL-Reranker-2B"
    rerank_api_key: str = ""
    rerank_mode: str = "text"
    rerank_top_k: int = 50
    rerank_score_threshold: float = 0.5
    rerank_batch_size: int = 4
    rerank_batch_concurrency: int = 4
    rerank_timeout_seconds: float = 120.0
    rerank_max_retries: int = 2
    rerank_max_inline_media_mb: float = 48.0

    # 高阈值 LLM merge 配置
    entity_merge_summary_enabled: bool = True
    entity_merge_summary_trigger_fragments: int = 24
    entity_merge_summary_trigger_tokens: int = 8000
    entity_merge_summary_max_output_tokens: int = 1200
    entity_merge_summary_context_size: int = 16000

    def __post_init__(self) -> None:
        for name in ("ER_extraction_max_async", "embedding_max_async"):
            value = getattr(self, name)
            if isinstance(value, str) and value.isdecimal():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            setattr(self, name, value)

    def get_task_limit(self, task_type: str) -> TaskLimitConfig:
        """获取指定任务类型的限制配置"""
        if self.task_limits is None:
            return TaskLimitConfig(max_concurrent=0, gpu_memory_mb=0)
        return self.task_limits.get(task_type, TaskLimitConfig(max_concurrent=0, gpu_memory_mb=0))


def load_config_from_yaml(config_path: str = "hyperrag.yml") -> HyperRAGConfig:
    """
    从 YAML 文件加载 HyperRAG 配置

    Args:
        config_path: 配置文件路径

    Returns:
        HyperRAGConfig 实例

    Example YAML structure:
        ```yaml
        working_dir: "./hyperrag-storage"
        workspace: "default"
        chunk_token_size: 512

        # 存储配置（支持嵌套或扁平）
        storage:
          kv_storage: "JsonKVStorage"
          vector_storage: "NanoVectorDBStorage"
          graph_storage: "NetworkXStorage"
          doc_status_storage: "JsonDocStatusStorage"

        # 任务管理配置
        enable_task_management: true
        redis_uri: "redis://localhost:6379/0"

        # 实例配置（支持嵌套或扁平）
        instance:
          id: "test-instance-001"
          initial_mode: "read_write"  # read_only 或 read_write
          heartbeat_interval: 5
          failover_timeout: 30

        # 任务限制
        task_limits:
          doc_parse:
            max_concurrent: 2
            gpu_memory_mb: 0
                    kg_index:
            max_concurrent: 4
            gpu_memory_mb: 0
        ```
    """
    config_file = Path(config_path)

    if not config_file.exists():
        logger.warning(f"Config file not found: {config_path}, using defaults")
        return HyperRAGConfig()

    with open(config_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data = _interpolate_environment(_strip_retired_video_embedding_config(data))

    logger.info(f"Loaded config from: {config_path}")

    def _resolve_config_path(value: str | None) -> str | None:
        if not value:
            return value
        path_value = Path(value)
        if path_value.is_absolute():
            return str(path_value)
        return str((config_file.parent / path_value).resolve())

    application_model_configs: Dict[str, Dict[str, Any]] = {}

    # 支持从 application_config_path 映射 embedding 等默认配置
    application_config_path = data.get("application_config_path")
    if application_config_path:
        resolved_application_config_path = _resolve_config_path(application_config_path)
        assert resolved_application_config_path is not None
        data["application_config_path"] = resolved_application_config_path
        application_model_configs = _load_application_model_configs(
            resolved_application_config_path
        )
        data = _merge_with_application_config(data, resolved_application_config_path)

    data = _populate_postgres_addon_params(data)

    # 直接使用数据创建配置对象
    # dataclass 会自动处理字段
    config_dict = {}

    # 基本配置
    for key in [
        "working_dir",
        "workspace",
        "chunk_token_size",
        "chunk_overlap_token_size",
        "respect_semantic_boundaries",
        "merge_small_tables_threshold_tokens",
        "enable_modal_replay",
        "entity_types",
        "asset_status_storage_backend",
        "mineru_method",
        "mineru_lang",
        "mineru_device",
        "mineru_backend",
        "mineru_formula",
        "mineru_table",
        "mineru_cache_dir",
        "mineru_timeout_seconds",
        "whisper_api_key",
        "whisper_base_url",
        "vision_model_max_concurrent",
        "video_enable_scene_detect",
        "video_cache_dir",
        "video_segment_length_sec",
        "video_clip_sample_fps",
        "embedding_params",
        "video_segment_output_format",
        "max_doc_parse_workers",
        "max_video_parse_workers",
        "max_index_workers",
        "max_concurrent_job_tasks",
        "enable_indexing",
        "enable_contextual_embeddings",
        "contextual_max_tokens",
        "contextual_window_chunks",
        # Actio 视频解析配置
        "actio_models_config_path",
        "actio_segment_config_path",
        "actio_sop_schema_path",
        "actio_device",
        "actio_asr_max_concurrency",
        "actio_annotate_max_concurrency",
        "actio_keep_asr_audio_outputs",
        # KG 核心配置
        "llm_model",
        "llm_base_url",
        "llm_api_key",
        "llm_model_kwargs",
        "embedding_model",
        "embedding_base_url",
        "embedding_api_key",
        "embedding_provider",
        "embedding_dim",
        "embedding_max_async",
        "llm_timeout",
        "embedding_timeout",
        "ER_extraction_max_async",
        "max_parallel_insert",
        "max_gleaning",
        "enable_llm_cache",
        "summary_max_tokens",
        "summary_context_size",
        "summary_length_recommended",
        "knowledgebase_summary_max_chars",
        "knowledge_discover_asset_matches_per_library",
        "knowledge_discover_asset_match_library_limit",
        "knowledge_discover_catalog_query_concurrency",
        "max_graph_nodes",
        "cosine_better_than_threshold",
        "addon_params",
        # BM25配置
        "bm25_enabled",
        "bm25_mode",
        "bm25_top_k",
        "hybrid_vector_weight",
        "hybrid_bm25_weight",
        "bm25_rrf_k",
        "bm25_stopwords_file",
        "bm25_custom_dict_file",
        "bm25_zhparser_dict_file",
        "bm25_zhparser_dict_table",
        "bm25_jieba_hmm",
        "bm25_storage_backend",
        # 外部应用配置
        "application_config_path",
        # KG向量库配置
        "kg_vector_enabled",
        "kg_vector_schema",
        "kg_vector_chunk_table",
        "kg_vector_entity_table",
        "kg_vector_relation_table",
        "kg_vector_query_top_k",
        "kg_vector_entity_top_k",
        "kg_vector_relation_top_k",
        "kg_vector_chunk_top_k",
        "vector_retrieval_top_k",
        "image_retrieval_top_k",
        "kg_vector_embedding_batch_size",
        "use_structured_video_er_extraction",
        # 查询期 synonym 扩展配置
        "query_synonym_expand_enabled",
        "query_synonym_expand_limit",
        "query_synonym_similarity_threshold",
        # Fast ingest / VKR / post-compile / lint
        "fast_ingest_enabled",
        "fast_ingest_compile_phase",
        "fast_ingest_model",
        "fast_ingest_provider",
        "fast_ingest_base_url",
        "fast_ingest_api_key",
        "fast_ingest_decoding_strategy",
        "fast_ingest_er_prompt_mode",
        "fast_ingest_er_max_output_tokens",
        "fast_ingest_er_description_max_chars",
        "fast_ingest_er_retry_full_on_empty",
        "precompile_enabled",
        "precompile_workspace",
        "precompile_refine_enabled",
        "precompile_refine_top_k",
        "precompile_refine_threshold",
        "vkr_enabled",
        "vkr_delta_top_k",
        "post_compile_auto_enabled",
        "post_compile_auto_interval_minutes",
        "post_compile_hot_buffer_edge_threshold",
        "post_compile_max_batches_per_cycle",
        "post_compile_batch_size",
        "lint_batch_size",
        "lint_auto_start_hour",
        "lint_auto_end_hour",
        # 查询期 rerank 配置
        "rerank_enabled",
        "rerank_base_url",
        "rerank_model",
        "rerank_api_key",
        "rerank_mode",
        "rerank_top_k",
        "rerank_score_threshold",
        "rerank_batch_size",
        "rerank_batch_concurrency",
        "rerank_timeout_seconds",
        "rerank_max_retries",
        "rerank_max_inline_media_mb",
        # 高阈值 LLM merge 配置
        "entity_merge_summary_enabled",
        "entity_merge_summary_trigger_fragments",
        "entity_merge_summary_trigger_tokens",
        "entity_merge_summary_max_output_tokens",
        "entity_merge_summary_context_size",
    ]:
        if key in data:
            config_dict[key] = data[key]

    for path_key in [
        "working_dir",
        "mineru_cache_dir",
        "video_cache_dir",
        "actio_models_config_path",
        "actio_segment_config_path",
        "actio_sop_schema_path",
        "bm25_stopwords_file",
        "bm25_custom_dict_file",
        "bm25_zhparser_dict_file",
    ]:
        if path_key in config_dict:
            config_dict[path_key] = _resolve_config_path(config_dict[path_key])

    # 支持嵌套的 storage 配置
    storage_config = data.get("storage", {})
    config_dict["kv_storage"] = storage_config.get("kv_storage") or data.get(
        "kv_storage", "PGKVStorage"
    )
    config_dict["vector_storage"] = storage_config.get("vector_storage") or data.get(
        "vector_storage", "PGVectorStorage"
    )
    config_dict["graph_storage"] = storage_config.get("graph_storage") or data.get(
        "graph_storage", "Neo4JStorage"
    )
    config_dict["doc_status_storage"] = storage_config.get("doc_status_storage") or data.get(
        "doc_status_storage", "PGDocStatusStorage"
    )

    # 任务管理配置
    config_dict["enable_task_management"] = data.get("enable_task_management", False)
    config_dict["enable_rw_lock"] = data.get("enable_rw_lock", False)
    config_dict["redis_uri"] = data.get("redis_uri", "redis://localhost:6379/0")

    # 支持嵌套的 instance 配置
    instance_config = data.get("instance", {})
    config_dict["instance_id"] = instance_config.get("id") or data.get("instance_id", "auto")
    config_dict["initial_mode"] = instance_config.get("initial_mode") or data.get(
        "initial_mode", "read_only"
    )
    config_dict["heartbeat_interval"] = instance_config.get("heartbeat_interval") or data.get(
        "heartbeat_interval", 5
    )
    config_dict["failover_timeout"] = instance_config.get("failover_timeout") or data.get(
        "failover_timeout", 30
    )

    # 任务限制
    if "task_limits" in data:
        task_limits = {k: TaskLimitConfig(**v) for k, v in data["task_limits"].items()}
        config_dict["task_limits"] = task_limits

    if data.get("max_concurrent_job_tasks") is not None:
        config_dict["max_concurrent_job_tasks"] = int(data["max_concurrent_job_tasks"])

    # 回调
    if "callbacks" in data:
        config_dict["callbacks"] = data["callbacks"]

    # 支持嵌套的 actio 配置
    actio_config = data.get("actio", {})
    config_dict["actio_models_config_path"] = actio_config.get("models_config_path") or data.get(
        "actio_models_config_path"
    )
    config_dict["actio_segment_config_path"] = actio_config.get("segment_config") or data.get(
        "actio_segment_config_path"
    )
    config_dict["actio_aggregate_config_path"] = actio_config.get("aggregate_config") or data.get(
        "actio_aggregate_config_path"
    )
    config_dict["actio_merge_config_path"] = actio_config.get("merge_config") or data.get(
        "actio_merge_config_path"
    )
    config_dict["actio_merge_dtw_config_path"] = actio_config.get("merge_dtw_config") or data.get(
        "actio_merge_dtw_config_path"
    )
    config_dict["actio_sop_schema_path"] = actio_config.get("sop_schema_path") or data.get(
        "actio_sop_schema_path"
    )
    config_dict["actio_device"] = actio_config.get("device") or data.get("actio_device", "cuda:1")
    config_dict["actio_asr_max_concurrency"] = int(
        actio_config.get("asr_max_concurrency") or data.get("actio_asr_max_concurrency", 8)
    )
    config_dict["actio_annotate_max_concurrency"] = int(
        actio_config.get("annotate_max_concurrency")
        or data.get("actio_annotate_max_concurrency", 8)
    )
    config_dict["actio_keep_asr_audio_outputs"] = _to_bool(
        actio_config.get("keep_asr_audio_outputs")
        if "keep_asr_audio_outputs" in actio_config
        else data.get("actio_keep_asr_audio_outputs", True)
    )
    for field_name in (
        "actio_models_config_path",
        "actio_segment_config_path",
        "actio_aggregate_config_path",
        "actio_merge_config_path",
        "actio_merge_dtw_config_path",
        "actio_sop_schema_path",
    ):
        value = config_dict.get(field_name)
        if value:
            config_dict[field_name] = _resolve_config_path(value)

    # 附加参数
    if "addon_params" in data:
        config_dict["addon_params"] = data["addon_params"]

    # 支持嵌套 models.fast_ingest 配置
    models_config = data.get("models") if isinstance(data.get("models"), dict) else {}
    llm_config = models_config.get("llm") if isinstance(models_config.get("llm"), dict) else {}
    embedding_config = (
        models_config.get("embedding") if isinstance(models_config.get("embedding"), dict) else {}
    )
    nested_model_mapping = {
        "llm_model": (llm_config, "model"),
        "llm_base_url": (llm_config, "base_url"),
        "llm_api_key": (llm_config, "api_key"),
        "embedding_model": (embedding_config, "model"),
        "embedding_provider": (embedding_config, "provider"),
        "embedding_base_url": (embedding_config, "base_url"),
        "embedding_api_key": (embedding_config, "api_key"),
        "embedding_params": (embedding_config, "params"),
        "embedding_max_async": (embedding_config, "max_async"),
        "embedding_dim": ((embedding_config.get("params") or {}), "embedding_dim"),
    }
    for target_key, (model_config, source_key) in nested_model_mapping.items():
        if target_key not in data and model_config.get(source_key) is not None:
            config_dict[target_key] = model_config[source_key]

    fast_ingest_config = (
        models_config.get("fast_ingest")
        if isinstance(models_config.get("fast_ingest"), dict)
        else {}
    )
    if fast_ingest_config:
        config_dict["fast_ingest_model"] = config_dict.get(
            "fast_ingest_model"
        ) or fast_ingest_config.get("model")
        config_dict["fast_ingest_provider"] = config_dict.get(
            "fast_ingest_provider"
        ) or fast_ingest_config.get("provider")
        config_dict["fast_ingest_base_url"] = config_dict.get(
            "fast_ingest_base_url"
        ) or fast_ingest_config.get("base_url")
        config_dict["fast_ingest_api_key"] = config_dict.get(
            "fast_ingest_api_key"
        ) or fast_ingest_config.get("api_key")

    # BM25配置（支持嵌套或扁平）
    bm25_config = data.get("bm25", {})
    config_dict["bm25_enabled"] = bm25_config.get("enabled") or data.get("bm25_enabled", False)
    config_dict["bm25_mode"] = bm25_config.get("mode") or data.get("bm25_mode", "simple")
    config_dict["bm25_top_k"] = bm25_config.get("top_k") or data.get("bm25_top_k", 30)
    config_dict["hybrid_vector_weight"] = bm25_config.get("vector_weight") or data.get(
        "hybrid_vector_weight", 0.5
    )
    config_dict["hybrid_bm25_weight"] = bm25_config.get("bm25_weight") or data.get(
        "hybrid_bm25_weight", 0.5
    )
    config_dict["bm25_rrf_k"] = bm25_config.get("rrf_k") or data.get("bm25_rrf_k", 60)
    config_dict["bm25_stopwords_file"] = bm25_config.get("stopwords_file") or data.get(
        "bm25_stopwords_file"
    )
    config_dict["bm25_custom_dict_file"] = bm25_config.get("custom_dict_file") or data.get(
        "bm25_custom_dict_file"
    )
    config_dict["bm25_zhparser_dict_file"] = bm25_config.get("zhparser_dict_file") or data.get(
        "bm25_zhparser_dict_file"
    )
    config_dict["bm25_zhparser_dict_table"] = bm25_config.get("zhparser_dict_table") or data.get(
        "bm25_zhparser_dict_table"
    )
    config_dict["bm25_jieba_hmm"] = (
        bm25_config.get("jieba_hmm")
        if bm25_config.get("jieba_hmm") is not None
        else data.get("bm25_jieba_hmm", True)
    )
    config_dict["bm25_storage_backend"] = bm25_config.get("storage_backend") or data.get(
        "bm25_storage_backend", "postgres"
    )
    config_dict["bm25_stopwords_file"] = _resolve_config_path(config_dict["bm25_stopwords_file"])
    config_dict["bm25_custom_dict_file"] = _resolve_config_path(
        config_dict["bm25_custom_dict_file"]
    )
    config_dict["bm25_zhparser_dict_file"] = _resolve_config_path(
        config_dict["bm25_zhparser_dict_file"]
    )

    # 检索配置（支持嵌套或扁平）
    retrieval_config = data.get("retrieval", {})
    if not isinstance(retrieval_config, dict):
        retrieval_config = {}
    vector_top_k = retrieval_config.get("vector_top_k")
    if vector_top_k is None:
        vector_top_k = retrieval_config.get("text_top_k")
    if vector_top_k is None:
        vector_top_k = retrieval_config.get("top_k")
    if vector_top_k is None:
        vector_top_k = retrieval_config.get("chunk_top_k")
    if vector_top_k is not None:
        config_dict["vector_retrieval_top_k"] = vector_top_k
        config_dict["kg_vector_chunk_top_k"] = vector_top_k
    if retrieval_config.get("bm25_top_k") is not None:
        config_dict["bm25_top_k"] = retrieval_config.get("bm25_top_k")
    if retrieval_config.get("image_top_k") is not None:
        config_dict["image_retrieval_top_k"] = retrieval_config.get("image_top_k")

    # 查询期 rerank 配置（支持嵌套或扁平）
    rerank_config = data.get("rerank", {})
    if not isinstance(rerank_config, dict):
        rerank_config = {}
    rerank_mapping = {
        "enabled": "rerank_enabled",
        "base_url": "rerank_base_url",
        "model": "rerank_model",
        "api_key": "rerank_api_key",
        "mode": "rerank_mode",
        "top_k": "rerank_top_k",
        "score_threshold": "rerank_score_threshold",
        "batch_size": "rerank_batch_size",
        "batch_concurrency": "rerank_batch_concurrency",
        "timeout_seconds": "rerank_timeout_seconds",
        "max_retries": "rerank_max_retries",
        "max_inline_media_mb": "rerank_max_inline_media_mb",
    }
    for rerank_key, target_key in rerank_mapping.items():
        if rerank_key in rerank_config and rerank_config[rerank_key] is not None:
            config_dict[target_key] = rerank_config[rerank_key]
    if rerank_config.get("text_top_k") is not None and rerank_config.get("top_k") is None:
        config_dict["rerank_top_k"] = rerank_config.get("text_top_k")

    # 统一调度（scheduler-core）配置：scheduler.unified 段
    scheduler_section = data.get("scheduler") if isinstance(data.get("scheduler"), dict) else {}
    unified_section = (
        scheduler_section.get("unified")
        if isinstance(scheduler_section.get("unified"), dict)
        else {}
    )
    if unified_section:
        if unified_section.get("max_concurrent_job_tasks") is not None:
            config_dict["max_concurrent_job_tasks"] = int(
                unified_section["max_concurrent_job_tasks"]
            )
        config_dict["scheduler_unified"] = UnifiedSchedulerConfig(
            enabled=_to_bool(unified_section.get("enabled", False)),
            redis_url=unified_section.get("redis_url", "redis://localhost:6379/0"),
            lease_ttl_seconds=int(unified_section.get("lease_ttl_seconds", 60)),
            heartbeat_interval_seconds=int(unified_section.get("heartbeat_interval_seconds", 15)),
            queue_wait_timeout_seconds=float(
                unified_section.get("queue_wait_timeout_seconds", 30.0)
            ),
            default_execution_timeout_seconds=float(
                unified_section.get("default_execution_timeout_seconds", 180.0)
            ),
            domains={
                k: (v if isinstance(v, dict) else {})
                for k, v in (unified_section.get("domains") or {}).items()
            },
        )

    config = HyperRAGConfig(**config_dict)
    if not config.rerank_api_key and config.embedding_api_key:
        config.rerank_api_key = config.embedding_api_key
    _attach_default_multimodal_funcs(config, application_model_configs)
    return config


def _merge_with_application_config(
    base_data: Dict[str, Any], application_config_path: str
) -> Dict[str, Any]:
    app_path = Path(application_config_path)
    if not app_path.exists():
        logger.warning("application config not found: %s", application_config_path)
        return base_data

    with open(app_path, "r", encoding="utf-8") as f:
        app_data = yaml.safe_load(f) or {}
    app_data = _interpolate_environment(
        _strip_retired_video_embedding_config(app_data, application=True)
    )

    merged = dict(base_data)
    hyperrag_cfg = app_data.get("hyperrag") or {}
    indexing_cfg = hyperrag_cfg.get("indexing") or {}
    models_cfg = indexing_cfg.get("models") or {}
    embedding_cfg = models_cfg.get("embedding") or {}
    base_models = base_data.get("models") or {}
    base_embedding = base_models.get("embedding") or {}
    app_env_cfg = hyperrag_cfg.get("env") or {}
    base_env_cfg = base_data.get("env") or {}
    redis_cfg = hyperrag_cfg.get("redis") or {}

    embedding_params = embedding_cfg.get("params") or {}

    defaults = {
        "embedding_model": embedding_cfg.get("model"),
        "embedding_provider": embedding_cfg.get("provider"),
        "embedding_base_url": embedding_cfg.get("base_url"),
        "embedding_api_key": embedding_cfg.get("api_key"),
        "embedding_dim": embedding_params.get("embedding_dim"),
        "embedding_params": embedding_params,
        "embedding_max_async": base_embedding.get("max_async", embedding_cfg.get("max_async")),
        "whisper_base_url": ((models_cfg.get("whisper") or {}).get("base_url")),
        "whisper_api_key": ((models_cfg.get("whisper") or {}).get("api_key")),
        "kg_vector_enabled": True,
    }

    if isinstance(app_env_cfg, dict) and isinstance(base_env_cfg, dict):
        merged["env"] = {
            **app_env_cfg,
            **base_env_cfg,
        }

    for key, value in defaults.items():
        if value is not None and key not in merged:
            merged[key] = value

    if "redis_uri" not in merged and redis_cfg.get("host"):
        redis_user = redis_cfg.get("username")
        redis_password = redis_cfg.get("password")
        redis_auth = ""
        if redis_user and redis_password:
            redis_auth = f"{redis_user}:{redis_password}@"
        elif redis_password:
            redis_auth = f":{redis_password}@"
        merged["redis_uri"] = (
            f"redis://{redis_auth}{redis_cfg.get('host')}:{redis_cfg.get('port', 6379)}/{redis_cfg.get('db', 0)}"
        )

    return merged


def _populate_postgres_addon_params(data: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(data)
    env_cfg = merged.get("env") or {}
    if not isinstance(env_cfg, dict):
        return merged

    postgres_parts = [
        env_cfg.get("POSTGRES_USER"),
        env_cfg.get("POSTGRES_PASSWORD"),
        env_cfg.get("POSTGRES_HOST"),
        env_cfg.get("POSTGRES_PORT"),
        env_cfg.get("POSTGRES_DATABASE"),
    ]
    if not all(postgres_parts):
        return merged

    existing_addon_params = merged.get("addon_params") or {}
    if not isinstance(existing_addon_params, dict):
        existing_addon_params = {}

    postgres_addon_params = {
        "postgres_user": env_cfg.get("POSTGRES_USER"),
        "postgres_password": env_cfg.get("POSTGRES_PASSWORD"),
        "postgres_host": env_cfg.get("POSTGRES_HOST"),
        "postgres_port": env_cfg.get("POSTGRES_PORT"),
        "postgres_database": env_cfg.get("POSTGRES_DATABASE"),
        "postgres_max_connections": env_cfg.get("POSTGRES_MAX_CONNECTIONS"),
    }
    merged["addon_params"] = {
        **postgres_addon_params,
        **existing_addon_params,
    }
    return merged


def _load_application_model_configs(application_config_path: str) -> Dict[str, Dict[str, Any]]:
    app_path = Path(application_config_path)
    if not app_path.exists():
        return {}

    with open(app_path, "r", encoding="utf-8") as f:
        app_data = yaml.safe_load(f) or {}
    app_data = _interpolate_environment(
        _strip_retired_video_embedding_config(app_data, application=True)
    )

    indexing_models = ((app_data.get("hyperrag") or {}).get("indexing") or {}).get("models") or {}
    if not isinstance(indexing_models, dict):
        return {}
    return {key: value for key, value in indexing_models.items() if isinstance(value, dict)}


def _attach_default_multimodal_funcs(
    config: HyperRAGConfig,
    application_model_configs: Dict[str, Dict[str, Any]],
) -> None:
    if config.vision_model_func is None:
        image_caption_model = (
            application_model_configs.get("image_caption")
            or application_model_configs.get("vlm")
            or application_model_configs.get("llm")
            or {}
        )
        config.vision_model_func = _build_image_caption_func(
            model_config=image_caption_model,
            fallback_model=config.llm_model,
            fallback_base_url=config.llm_base_url,
            fallback_api_key=config.llm_api_key,
            default_timeout=config.llm_timeout,
        )

    if config.video_caption_func is None:
        video_caption_model = (
            application_model_configs.get("video_caption")
            or application_model_configs.get("vlm")
            or application_model_configs.get("llm")
            or {}
        )
        config.video_caption_func = _build_video_caption_func(
            model_config=video_caption_model,
            fallback_model=config.llm_model,
            fallback_base_url=config.llm_base_url,
            fallback_api_key=config.llm_api_key,
            default_timeout=config.llm_timeout,
        )


def _strip_retired_video_embedding_config(
    data: Dict[str, Any], *, application: bool = False
) -> Dict[str, Any]:
    def strip_section(section: Dict[str, Any]) -> Dict[str, Any]:
        removed = {"max_async", "embedding_batch_num", "embedding_func_max_async"} & section.keys()
        if removed:
            raise ValueError(
                f"Removed HyperRAG configuration keys: {', '.join(sorted(removed))}; "
                "use ER_extraction_max_async and models.embedding.max_async"
            )
        cleaned = {
            key: value
            for key, value in section.items()
            if not key.startswith("video_embedding_")
            and key not in {"enable_video_embedding", "video_segment_storage_backend"}
        }
        models = cleaned.get("models")
        if isinstance(models, dict):
            cleaned["models"] = {
                key: value
                for key, value in models.items()
                if key not in {"video_embedding", "vision_embedding"}
            }
        return cleaned

    if not application:
        return strip_section(data)
    cleaned = dict(data)
    hyperrag = cleaned.get("hyperrag")
    if isinstance(hyperrag, dict):
        hyperrag = strip_section(hyperrag)
        if isinstance(hyperrag.get("indexing"), dict):
            hyperrag["indexing"] = strip_section(hyperrag["indexing"])
        cleaned["hyperrag"] = hyperrag
    return cleaned


def build_default_multimodal_embedder(config: HyperRAGConfig):
    model = str(config.embedding_model or "").strip()
    base_url = str(config.embedding_base_url or "").strip()
    if not model or not base_url:
        return None

    from .multimodal_embedder import OpenAICompatibleMultimodalEmbedder

    params = config.embedding_params
    if params.get("local_video_mode", "video_base64") != "video_base64":
        raise ValueError("Unified video embedding requires local_video_mode=video_base64")
    return OpenAICompatibleMultimodalEmbedder(
        base_url=base_url,
        model=model,
        api_key=str(config.embedding_api_key or ""),
        provider=str(config.embedding_provider or "vllm"),
        timeout_sec=float(params.get("timeout", config.embedding_timeout or 120)),
        local_video_mode=str(params.get("local_video_mode", "video_base64")),
        max_inline_image_bytes=int(params.get("max_inline_image_bytes", 10 * 1024 * 1024)),
        max_inline_video_bytes=int(params.get("max_inline_video_bytes", 30 * 1024 * 1024)),
    )


def _build_image_caption_func(
    *,
    model_config: Dict[str, Any],
    fallback_model: str,
    fallback_base_url: str,
    fallback_api_key: Optional[str],
    default_timeout: int,
):
    resolved = _resolve_model_config(
        model_config=model_config,
        fallback_model=fallback_model,
        fallback_base_url=fallback_base_url,
        fallback_api_key=fallback_api_key,
        default_timeout=default_timeout,
    )
    if resolved is None:
        return None

    from .model_func import ImageCaptionFunc, ModelConfig

    return ImageCaptionFunc(
        ModelConfig(**resolved),
        max_tokens=_extract_max_tokens(model_config),
    )


def _build_video_caption_func(
    *,
    model_config: Dict[str, Any],
    fallback_model: str,
    fallback_base_url: str,
    fallback_api_key: Optional[str],
    default_timeout: int,
):
    resolved = _resolve_model_config(
        model_config=model_config,
        fallback_model=fallback_model,
        fallback_base_url=fallback_base_url,
        fallback_api_key=fallback_api_key,
        default_timeout=default_timeout,
    )
    if resolved is None:
        return None

    from .model_func import ModelConfig, VideoCaptionFunc

    return VideoCaptionFunc(
        ModelConfig(**resolved),
        max_tokens=_extract_max_tokens(model_config),
    )


def _resolve_model_config(
    *,
    model_config: Dict[str, Any],
    fallback_model: str,
    fallback_base_url: str,
    fallback_api_key: Optional[str],
    default_timeout: int,
) -> Optional[Dict[str, Any]]:
    model = str(model_config.get("model") or fallback_model or "").strip()
    base_url = str(model_config.get("base_url") or fallback_base_url or "").strip()
    api_key = model_config.get("api_key") or fallback_api_key
    provider = str(model_config.get("provider") or _infer_provider(base_url)).strip()
    params = model_config.get("params") or {}

    if not model or not base_url or not api_key:
        return None

    timeout = default_timeout
    if isinstance(params, dict):
        try:
            timeout = int(params.get("timeout", default_timeout))
        except (TypeError, ValueError):
            timeout = default_timeout

    return {
        "model": model,
        "base_url": base_url,
        "api_key": str(api_key),
        "provider": provider,
        "timeout": timeout,
    }


def _extract_max_tokens(model_config: Dict[str, Any], default: int = 1024) -> int:
    params = model_config.get("params") or {}
    value = params.get("max_tokens", default) if isinstance(params, dict) else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _infer_provider(base_url: str) -> str:
    normalized = str(base_url or "").strip().lower().rstrip("/")
    if normalized.endswith("api.openai.com/v1") or "api.openai.com" in normalized:
        return "openai"
    return "vllm"


def get_default_task_limits() -> Dict[str, Dict[str, int]]:
    """
    获取默认的任务限制配置

    Returns:
        任务限制字典
    """
    return {
        "doc_parse": {"max_concurrent": 2, "gpu_memory_mb": 0},
        "video_asr": {"max_concurrent": 2, "gpu_memory_mb": 0},
        "video_caption": {"max_concurrent": 2, "gpu_memory_mb": 0},
        "video_tracking": {"max_concurrent": 1, "gpu_memory_mb": 8192},
        "chunking": {"max_concurrent": 4, "gpu_memory_mb": 0},
        "kg_index": {"max_concurrent": 4, "gpu_memory_mb": 0},
    }
