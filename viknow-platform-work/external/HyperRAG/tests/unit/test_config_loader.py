from __future__ import annotations

from pathlib import Path

import pytest
from hyperrag.config_loader import (
    EnvironmentInterpolationError,
    HyperRAGConfig,
    build_default_multimodal_embedder,
    load_config_from_yaml,
)


def test_load_config_from_yaml_interpolates_environment_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CONFIG_API_KEY", "resolved-test-key")
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text("llm_api_key: ${TEST_CONFIG_API_KEY}\n", encoding="utf-8")

    config = load_config_from_yaml(str(config_path))

    assert config.llm_api_key == "resolved-test-key"


def test_load_config_from_yaml_reports_missing_variable_without_secret(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-appear")
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text("llm_api_key: ${MISSING_CONFIG_API_KEY}\n", encoding="utf-8")

    with pytest.raises(EnvironmentInterpolationError, match="MISSING_CONFIG_API_KEY") as error:
        load_config_from_yaml(str(config_path))

    assert "must-not-appear" not in str(error.value)


def test_load_config_from_yaml_maps_interpolated_nested_model_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NESTED_LLM_KEY", "nested-llm-key")
    monkeypatch.setenv("NESTED_EMBEDDING_KEY", "nested-embedding-key")
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "models:",
                "  llm:",
                "    model: native-llm",
                "    base_url: http://llm.example/v1",
                "    api_key: ${NESTED_LLM_KEY}",
                "  embedding:",
                "    model: native-embedding",
                "    provider: vllm",
                "    base_url: http://embedding.example/v1",
                "    api_key: ${NESTED_EMBEDDING_KEY}",
                "    params:",
                "      embedding_dim: 12",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.llm_model == "native-llm"
    assert config.llm_api_key == "nested-llm-key"
    assert config.embedding_model == "native-embedding"
    assert config.embedding_api_key == "nested-embedding-key"
    assert config.embedding_dim == 12


def test_load_config_maps_actio_annotate_concurrency(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text("actio:\n  annotate_max_concurrency: 2\n", encoding="utf-8")

    config = load_config_from_yaml(str(config_path))

    assert config.actio_annotate_max_concurrency == 2


def test_default_config_enables_native_chunk_vector_retrieval() -> None:
    assert HyperRAGConfig().kg_vector_enabled is True


@pytest.mark.parametrize("application", [False, True])
def test_retired_video_config_is_pruned_before_env_interpolation(
    tmp_path, monkeypatch, application
):
    monkeypatch.delenv("RETIRED_VIDEO_TOKEN", raising=False)
    config_path = tmp_path / "hyperrag.yml"
    retired = (
        "enable_video_embedding: true\n"
        "video_embedding_api_key: ${RETIRED_VIDEO_TOKEN}\n"
        "video_embedding_params:\n  api_key: obsolete-literal\n"
        "models:\n  vision_embedding:\n    api_key: ${RETIRED_VIDEO_TOKEN}\n"
    )
    if application:
        app_path = tmp_path / "application.yml"
        app_path.write_text(
            "hyperrag:\n  indexing:\n" + "\n".join("    " + line for line in retired.splitlines()),
        )
        original = app_path.read_text()
        config_path.write_text(f"application_config_path: {app_path.name}\n")
    else:
        config_path.write_text(retired)
        original = config_path.read_text()
    config = load_config_from_yaml(str(config_path))
    assert not hasattr(config, "video_embedding_api_key")
    assert "obsolete-literal" not in repr(config)
    assert (app_path if application else config_path).read_text() == original


def test_default_config_enables_contextual_embeddings() -> None:
    assert HyperRAGConfig().enable_contextual_embeddings is True


def test_default_config_uses_shared_embedding_concurrency() -> None:
    config = HyperRAGConfig()
    assert config.embedding_max_async == 32
    assert config.ER_extraction_max_async == 8
    assert not hasattr(config, "embedding_batch_num")
    assert not hasattr(config, "embedding_func_max_async")
    assert not hasattr(config, "max_async")


def test_load_config_from_yaml_reads_embedding_concurrency(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "ER_extraction_max_async: 3\nmodels:\n  embedding:\n    max_async: 6\n",
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.embedding_max_async == 6
    assert config.ER_extraction_max_async == 3


@pytest.mark.parametrize("key", ["max_async", "embedding_batch_num", "embedding_func_max_async"])
def test_load_config_rejects_removed_concurrency_keys(tmp_path, key):
    path = tmp_path / "hyperrag.yml"
    path.write_text(f"{key}: 4\n")
    with pytest.raises(ValueError, match=key):
        load_config_from_yaml(str(path))


@pytest.mark.parametrize("key", ["ER_extraction_max_async", "embedding_max_async"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_concurrency_requires_positive_integer(key, value):
    with pytest.raises(ValueError, match=key):
        HyperRAGConfig(**{key: value})


def test_embedding_concurrency_supports_numeric_environment_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_EMBEDDING_CONCURRENCY", "5")
    path = tmp_path / "hyperrag.yml"
    path.write_text("models:\n  embedding:\n    max_async: ${TEST_EMBEDDING_CONCURRENCY}\n")
    assert load_config_from_yaml(str(path)).embedding_max_async == 5


@pytest.mark.parametrize("override,expected", [("", 7), ("    max_async: 3\n", 3)])
def test_application_embedding_concurrency_respects_local_override(tmp_path, override, expected):
    app = tmp_path / "application.yml"
    app.write_text("hyperrag:\n  indexing:\n    models:\n      embedding:\n        max_async: 7\n")
    path = tmp_path / "hyperrag.yml"
    path.write_text(
        f"application_config_path: {app.name}\nmodels:\n  embedding:\n    dim: 2\n{override}"
    )
    assert load_config_from_yaml(str(path)).embedding_max_async == expected


def test_load_config_from_yaml_attaches_default_multimodal_funcs(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "llm_model: Qwen/Qwen3.6-35B-A3B-FP8",
                "llm_base_url: http://127.0.0.1:8500/v1",
                "llm_api_key: test-key",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert callable(config.vision_model_func)
    assert callable(config.video_caption_func)


def test_load_config_from_yaml_preserves_whisper_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "whisper_base_url: http://ai.qingxiang.tech:4000/v1",
                "whisper_api_key: whisper-test-key",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.whisper_base_url == "http://ai.qingxiang.tech:4000/v1"
    assert config.whisper_api_key == "whisper-test-key"


def test_load_config_from_yaml_sets_structured_video_er_switch(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "use_structured_video_er_extraction: true\n",
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.use_structured_video_er_extraction is True


def test_load_config_from_yaml_sets_fast_ingest_er_tuning_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "fast_ingest_er_prompt_mode: compact",
                "fast_ingest_er_max_output_tokens: 128",
                "fast_ingest_er_description_max_chars: 48",
                "fast_ingest_er_retry_full_on_empty: false",
                "post_compile_auto_enabled: true",
                "post_compile_auto_interval_minutes: 20",
                "post_compile_hot_buffer_edge_threshold: 6000",
                "post_compile_max_batches_per_cycle: 3",
                "post_compile_batch_size: 4000",
                "lint_batch_size: 120",
                "lint_auto_start_hour: 2",
                "lint_auto_end_hour: 8",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.fast_ingest_er_prompt_mode == "compact"
    assert config.fast_ingest_er_max_output_tokens == 128
    assert config.fast_ingest_er_description_max_chars == 48
    assert config.fast_ingest_er_retry_full_on_empty is False
    assert config.post_compile_auto_enabled is True
    assert config.post_compile_auto_interval_minutes == 20
    assert config.post_compile_hot_buffer_edge_threshold == 6000
    assert config.post_compile_max_batches_per_cycle == 3
    assert config.post_compile_batch_size == 4000
    assert config.lint_batch_size == 120
    assert config.lint_auto_start_hour == 2
    assert config.lint_auto_end_hour == 8


def test_load_config_from_yaml_fast_ingest_retry_default_off(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text("", encoding="utf-8")

    config = load_config_from_yaml(str(config_path))

    assert config.fast_ingest_er_retry_full_on_empty is False
    assert config.fast_ingest_er_max_output_tokens == 512


def test_load_config_from_yaml_sets_knowledgebase_summary_max_chars(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "knowledgebase_summary_max_chars: 8000",
                "knowledge_discover_asset_matches_per_library: 3",
                "knowledge_discover_asset_match_library_limit: 20",
                "knowledge_discover_catalog_query_concurrency: 4",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.knowledgebase_summary_max_chars == 8000
    assert config.knowledge_discover_asset_matches_per_library == 3
    assert config.knowledge_discover_asset_match_library_limit == 20
    assert config.knowledge_discover_catalog_query_concurrency == 4
    assert not hasattr(config, "knowledge_discover_result_top_k")


def test_load_config_from_yaml_resolves_actio_segment_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "local"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "actio:",
                "  aggregate_config: ./actio/aggregate.yaml",
                "  merge_config: ./actio/merge.yaml",
                "  merge_dtw_config: ./actio/merge.dtw.yaml",
                "  sop_schema_path: ./actio/sop_schema.md",
                "  segment_config: ../../external/actio/configs/fixed_segment.yaml",
                "  models_config_path: ./actio/model_calls.yaml",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.actio_segment_config_path == str(
        tmp_path / "external" / "actio" / "configs" / "fixed_segment.yaml"
    )
    for field, name in (
        ("actio_aggregate_config_path", "aggregate.yaml"),
        ("actio_merge_config_path", "merge.yaml"),
        ("actio_merge_dtw_config_path", "merge.dtw.yaml"),
        ("actio_sop_schema_path", "sop_schema.md"),
    ):
        assert getattr(config, field) == str(config_dir / "actio" / name)
    assert config.actio_models_config_path == str(
        tmp_path / "config" / "local" / "actio" / "model_calls.yaml"
    )


def test_load_config_from_yaml_maps_unified_embedding_from_application_config(
    tmp_path: Path,
) -> None:
    app_config_path = tmp_path / "application.yml"
    app_config_path.write_text(
        "\n".join(
            [
                "hyperrag:",
                "  indexing:",
                "    models:",
                "      embedding:",
                "        provider: vllm",
                "        model: Qwen/Qwen3-VL-Embedding-2B",
                "        params:",
                "          embedding_dim: 1024",
                "          local_video_mode: video_base64",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        f"application_config_path: {app_config_path.name}\n",
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.embedding_model == "Qwen/Qwen3-VL-Embedding-2B"
    assert config.embedding_provider == "vllm"
    assert config.embedding_params["local_video_mode"] == "video_base64"


def test_load_config_from_yaml_maps_embedding_connection_fields(
    tmp_path: Path,
) -> None:
    app_config_path = tmp_path / "application.yml"
    app_config_path.write_text(
        "\n".join(
            [
                "hyperrag:",
                "  indexing:",
                "    models:",
                "      embedding:",
                "        provider: vllm",
                "        model: Qwen/Qwen3-VL-Embedding-2B",
                "        base_url: http://embed.example/v1",
                "        api_key: embed-key",
                "        params:",
                "          embedding_dim: 1024",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        f"application_config_path: {app_config_path.name}\n",
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.embedding_base_url == "http://embed.example/v1"
    assert config.embedding_api_key == "embed-key"


def test_load_config_from_yaml_builds_postgres_addon_params_from_env(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "env:",
                "  POSTGRES_USER: postgres",
                "  POSTGRES_PASSWORD: local-pass",
                "  POSTGRES_HOST: localhost",
                '  POSTGRES_PORT: "15432"',
                "  POSTGRES_DATABASE: viknow2",
                '  POSTGRES_MAX_CONNECTIONS: "1000"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.addon_params["postgres_user"] == "postgres"
    assert config.addon_params["postgres_password"] == "local-pass"
    assert config.addon_params["postgres_host"] == "localhost"
    assert config.addon_params["postgres_port"] == "15432"
    assert config.addon_params["postgres_database"] == "viknow2"
    assert config.addon_params["postgres_max_connections"] == "1000"


def test_load_config_from_yaml_prefers_hyperrag_env_over_application_env(
    tmp_path: Path,
) -> None:
    app_config_path = tmp_path / "application.yml"
    app_config_path.write_text(
        "\n".join(
            [
                "hyperrag:",
                "  env:",
                "    POSTGRES_USER: postgres",
                "    POSTGRES_PASSWORD: app-pass",
                "    POSTGRES_HOST: app-host",
                '    POSTGRES_PORT: "15432"',
                "    POSTGRES_DATABASE: viknow",
                '    POSTGRES_MAX_CONNECTIONS: "20"',
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                f"application_config_path: {app_config_path.name}",
                "env:",
                "  POSTGRES_USER: postgres",
                "  POSTGRES_PASSWORD: local-pass",
                "  POSTGRES_HOST: localhost",
                '  POSTGRES_PORT: "25432"',
                "  POSTGRES_DATABASE: viknow2",
                '  POSTGRES_MAX_CONNECTIONS: "1000"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.addon_params["postgres_password"] == "local-pass"
    assert config.addon_params["postgres_host"] == "localhost"
    assert config.addon_params["postgres_port"] == "25432"
    assert config.addon_params["postgres_database"] == "viknow2"
    assert config.addon_params["postgres_max_connections"] == "1000"


def test_load_config_from_yaml_reuses_application_embedding_for_video_embedding(
    tmp_path: Path,
) -> None:
    app_config_path = tmp_path / "application.yml"
    app_config_path.write_text(
        "\n".join(
            [
                "hyperrag:",
                "  indexing:",
                "    models:",
                "      embedding:",
                "        provider: vllm",
                "        model: Qwen/Qwen3-VL-Embedding-2B",
                "        base_url: http://embed.example/v1",
                "        api_key: embed-key",
                "        params:",
                "          embedding_dim: 1024",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        f"application_config_path: {app_config_path.name}\n",
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    embedder = build_default_multimodal_embedder(config)
    assert embedder.model == config.embedding_model
    assert embedder.provider == config.embedding_provider
    assert embedder.base_url == config.embedding_base_url
    assert embedder.api_key == config.embedding_api_key
    assert not hasattr(config, "video_embedding_dim")


def test_load_config_from_yaml_reuses_direct_embedding_for_video_embedding(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "embedding_provider: vllm",
                "embedding_model: Qwen/Qwen3-VL-Embedding-2B",
                "embedding_base_url: http://embed.example/v1",
                "embedding_api_key: embed-key",
                "embedding_dim: 1024",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    embedder = build_default_multimodal_embedder(config)
    assert embedder.model == "Qwen/Qwen3-VL-Embedding-2B"
    assert embedder.provider == "vllm"
    assert embedder.base_url == "http://embed.example/v1"
    assert embedder.api_key == "embed-key"
    assert config.embedding_dim == 1024


def test_build_default_multimodal_embedder_uses_remote_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "embedding_provider: vllm",
                "embedding_model: Qwen/Qwen3-VL-Embedding-2B",
                "embedding_base_url: http://embed.example/v1",
                "embedding_api_key: embed-key",
                "embedding_params:",
                "  local_video_mode: video_base64",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))
    embedder = build_default_multimodal_embedder(config)

    assert embedder is not None
    assert callable(embedder.embed_video)
    assert callable(embedder.embed_text)
    assert callable(embedder.embed_image)


def test_load_config_from_yaml_loads_rerank_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "rerank_enabled: true",
                "rerank_base_url: http://rerank.example/v1",
                "rerank_model: demo-reranker",
                "rerank_api_key: rerank-key",
                "rerank_top_k: 12",
                "rerank_score_threshold: 0.7",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.rerank_enabled is True
    assert config.rerank_mode == "text"
    assert config.rerank_base_url == "http://rerank.example/v1"
    assert config.rerank_model == "demo-reranker"
    assert config.rerank_api_key == "rerank-key"
    assert config.rerank_top_k == 12
    assert config.rerank_score_threshold == 0.7


def test_load_config_from_yaml_maps_nested_retrieval_and_rerank_top_k(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "retrieval:",
                "  vector_top_k: 18",
                "  bm25_top_k: 31",
                "  image_top_k: 7",
                "rerank:",
                "  enabled: true",
                "  top_k: 9",
                "  score_threshold: 0.6",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.vector_retrieval_top_k == 18
    assert config.bm25_top_k == 31
    assert config.image_retrieval_top_k == 7
    assert config.rerank_enabled is True
    assert config.rerank_top_k == 9
    assert config.rerank_score_threshold == 0.6


def test_load_config_from_yaml_enables_rerank_by_default(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text("", encoding="utf-8")

    config = load_config_from_yaml(str(config_path))

    assert config.rerank_enabled is True
    assert config.rerank_top_k == 50
    assert config.rerank_score_threshold == 0.5
    assert config.image_retrieval_top_k == 3


def test_load_config_from_yaml_uses_embedding_key_as_rerank_key_by_default(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "embedding_api_key: shared-key",
                "rerank_enabled: true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.rerank_api_key == "shared-key"


def test_load_config_from_yaml_maps_models_fast_ingest_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text(
        "\n".join(
            [
                "models:",
                "  fast_ingest:",
                "    model: Qwen/Qwen3-4B-Instruct-FP8",
                "    provider: vllm",
                "    base_url: http://127.0.0.1:8505/v1",
                "    api_key: test-fast-ingest-key",
                "fast_ingest_enabled: true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config_from_yaml(str(config_path))

    assert config.fast_ingest_enabled is True
    assert config.fast_ingest_model == "Qwen/Qwen3-4B-Instruct-FP8"
    assert config.fast_ingest_provider == "vllm"
    assert config.fast_ingest_base_url == "http://127.0.0.1:8505/v1"
    assert config.fast_ingest_api_key == "test-fast-ingest-key"
    assert config.fast_ingest_decoding_strategy == "single_model"


def test_missing_actio_paths_remain_unset(tmp_path):
    config_path = tmp_path / "hyperrag.yml"
    config_path.write_text("", encoding="utf-8")
    config = load_config_from_yaml(str(config_path))
    for name in ("models", "segment", "aggregate", "merge", "merge_dtw"):
        assert getattr(config, f"actio_{name}_config_path") is None
    assert config.actio_sop_schema_path is None
