from __future__ import annotations

import json

import pytest
from hyperrag.single_layer_kg import (
    COMPACT_ER_MAX_ENTITIES,
    COMPACT_ER_MAX_RELATIONS,
    ChunkRecord,
    DocumentGraph,
    ExtractionAttempt,
    ExtractionParseResult,
    HyperRAGConfig,
    SingleLayerKGExtractor,
    SingleLayerKGService,
    _classify_text_generation,
    _extract_chunk_text,
)


def _demo_chunk() -> ChunkRecord:
    return ChunkRecord(
        chunk_id="chunk-1",
        file_path="/tmp/demo.md",
        order=0,
        raw_content="原文",
        text="门锁连接门机，检修阶段检查门锁状态。",
        tokens=32,
        segment_ids=["seg-1"],
    )


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (
            "<\u6587\u6863\u5185\u5bb9>x</\u6587\u6863\u5185\u5bb9> \u7b80\u77ed\u7684\u6458\u8981",
            ("hyperrag_document_summary", "document_summary"),
        ),
        (
            "\u751f\u6210\u8fd9\u4e2a chunk \u7684\u6807\u9898",
            ("hyperrag_chunk_title", "chunk_title"),
        ),
        (
            "\u4e0a\u4e0b\u6587\u8bf4\u660e <chunk>x</chunk>",
            ("hyperrag_chunk_context", "chunk_context"),
        ),
        ("ordinary request", ("kg_llm", "general")),
    ],
)
def test_classify_text_generation(prompt: str, expected: tuple[str, str]) -> None:
    assert _classify_text_generation(prompt) == expected


def test_extract_chunk_text_preserves_context_for_vector_content() -> None:
    raw_content = json.dumps(
        {
            "context": "This chunk is in Chapter 2.1 / Attractions.",
            "content": "ESCAPE BRYGGEN does not show the wheelchair icon.",
        }
    )

    assert _extract_chunk_text(raw_content) == (
        '### context\n\n"This chunk is in Chapter 2.1 / Attractions."\n\n'
        '### chunk text\n\n"ESCAPE BRYGGEN does not show the wheelchair icon."'
    )


def test_extract_chunk_text_keeps_original_content_without_context() -> None:
    raw_content = json.dumps({"content": "Plain chunk content."})

    assert _extract_chunk_text(raw_content) == "Plain chunk content."


@pytest.mark.asyncio
async def test_chunk_vector_rows_embed_and_persist_contextual_text() -> None:
    contextual_text = _extract_chunk_text(
        json.dumps(
            {
                "context": "This chunk is in Chapter 2.1 / Attractions.",
                "content": "ESCAPE BRYGGEN does not show the wheelchair icon.",
            }
        )
    )
    chunk = ChunkRecord(
        chunk_id="chunk-contextual",
        file_path="/tmp/brochure.pdf",
        order=4,
        raw_content="",
        text=contextual_text,
        tokens=16,
        segment_ids=["segment-4"],
        metadata={"chunk_type": "image"},
    )
    graph = DocumentGraph(
        workspace="demo",
        file_path=chunk.file_path,
        source_uri=chunk.file_path,
        modality="document",
        title="Bergen brochure",
        asset={},
        segments=[],
        chunks=[chunk],
        extracted_entities=[],
        extracted_relations=[],
        entity_nodes=[],
        relation_edges=[],
    )
    service = SingleLayerKGService(HyperRAGConfig(workspace="demo", embedding_dim=1))

    class EmbeddingClient:
        def __init__(self) -> None:
            self.inputs: list[str] = []

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.inputs = texts
            return [[0.1]]

    embedding_client = EmbeddingClient()
    service.embedding_client = embedding_client

    rows = await service._build_chunk_vector_rows(graph)  # noqa: SLF001

    assert embedding_client.inputs == [contextual_text]
    assert rows[0]["content"] == contextual_text
    assert rows[0]["metadata"]["text"] == contextual_text


def test_compact_prompt_is_minified_json() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=128,
        entity_description_max_chars=48,
    )
    prompt = extractor._build_prompt(chunk=_demo_chunk(), modality="document", title="demo")  # noqa: SLF001

    assert "\n" not in prompt
    assert len(prompt) < 900
    assert '"relations"' in prompt
    assert '"src"' in prompt
    assert '"source_entity"' not in prompt


def test_compact_response_schema_enforces_array_caps() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )

    response_format = extractor._response_format(prompt_mode="compact")  # noqa: SLF001
    schema = response_format["json_schema"]["schema"]

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert schema["properties"]["relations"]["maxItems"] == COMPACT_ER_MAX_RELATIONS
    assert "entities" not in schema["properties"]
    assert list(schema["properties"]) == ["relations"]


def test_build_assertions_supports_short_schema_triples() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=128,
        entity_description_max_chars=48,
    )
    entities, relations = extractor._build_assertions(  # noqa: SLF001
        chunk=_demo_chunk(),
        payload={
            "entities": [{"name": "门锁", "type": "设备", "desc": "门锁组件"}],
            "triples": [{"s": "门锁", "p": "连接", "o": "门机", "desc": "检修阶段"}],
        },
    )

    names = {entity.entity_name for entity in entities}
    assert "门锁" in names
    assert "门机" in names
    assert any(
        entity.entity_name == "门锁" and entity.entity_description == "门锁组件"
        for entity in entities
    )
    assert len(relations) == 1
    assert relations[0].source_entity == "门锁"
    assert relations[0].target_entity == "门机"
    assert relations[0].relationship_description == "连接"
    assert relations[0].situation == "检修阶段"


def test_build_assertions_supports_compact_src_tgt_schema() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )

    entities, relations = extractor._build_assertions(  # noqa: SLF001
        chunk=_demo_chunk(),
        payload={
            "entities": [],
            "relations": [
                {
                    "src": "门锁",
                    "tgt": "门机",
                    "description": "连接",
                    "context": "检修阶段",
                }
            ],
        },
    )

    assert {entity.entity_name for entity in entities} == {"门锁", "门机"}
    assert len(relations) == 1
    assert relations[0].source_entity == "门锁"
    assert relations[0].target_entity == "门机"
    assert relations[0].situation == "检修阶段"


def test_parse_json_recovers_from_fenced_and_truncated_content() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=192,
        entity_description_max_chars=48,
    )

    fenced = '```json\n{"entities":[{"name":"门锁"}],"relations":[]}\n```'
    truncated = '{"entities":[{"name":"门锁"}],"relations":[{"s":"门锁","p":"连接","o":"门机"}]'

    parsed_fenced = extractor._parse_json(fenced)  # noqa: SLF001
    parsed_truncated = extractor._parse_json(truncated)  # noqa: SLF001

    assert isinstance(parsed_fenced, dict)
    assert "entities" in parsed_fenced
    assert isinstance(parsed_truncated, dict)
    assert "relations" in parsed_truncated

    diagnostics = extractor._parse_json_with_diagnostics(truncated)  # noqa: SLF001
    assert diagnostics.repair_used is True
    assert diagnostics.parse_mode == "repaired_json"


def test_parse_json_repairs_nested_compact_payload_in_closing_order() -> None:
    truncated = (
        '{"relations":[{"src":"门锁","tgt":"门机","description":"连接","context":"检修阶段"}'
    )

    diagnostics = SingleLayerKGExtractor._parse_json_with_diagnostics(truncated)  # noqa: SLF001

    assert diagnostics.repair_used is True
    assert diagnostics.payload["relations"][0]["src"] == "门锁"


def test_parse_json_supports_flat_compact_relation_object() -> None:
    flat_relation = '{"src":"门锁","tgt":"门机","description":"连接","context":"检修阶段"}'

    parsed = SingleLayerKGExtractor._parse_json(flat_relation)  # noqa: SLF001

    assert parsed["entities"] == []
    assert parsed["relations"] == [
        {
            "src": "门锁",
            "tgt": "门机",
            "description": "连接",
            "context": "检修阶段",
        }
    ]


def test_parse_json_recovers_from_think_wrapped_payload() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=192,
        entity_description_max_chars=48,
    )
    wrapped = (
        '<think>analysis</think>{"entities":[{"name":"门锁"}],'
        '"relations":[{"s":"门锁","p":"连接","o":"门机"}]}'
    )

    parsed = extractor._parse_json(wrapped)  # noqa: SLF001

    assert isinstance(parsed, dict)
    assert "entities" in parsed
    assert "relations" in parsed


def test_build_assertions_falls_back_entity_description_to_name() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=128,
        entity_description_max_chars=48,
    )
    entities, _ = extractor._build_assertions(  # noqa: SLF001
        chunk=_demo_chunk(),
        payload={"entities": [{"name": "门锁", "type": "设备", "desc": ""}], "relations": []},
    )

    assert len(entities) == 1
    assert entities[0].entity_name == "门锁"
    assert entities[0].entity_description == "门锁"


def test_build_assertions_full_mode_does_not_fallback_entity_description() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="full",
        allow_entity_description_fallback=False,
    )
    entities, _ = extractor._build_assertions(  # noqa: SLF001
        chunk=_demo_chunk(),
        payload={"entities": [{"name": "门锁", "type": "设备", "desc": ""}], "relations": []},
    )

    assert entities == []


@pytest.mark.asyncio
async def test_full_mode_retries_when_required_fields_missing(monkeypatch) -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="full",
        strict_required_fields=True,
        required_field_retry_attempts=3,
        allow_entity_description_fallback=False,
        allow_relation_entity_inference=False,
    )
    calls = 0

    async def fake_extract_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = {
                "entities": [
                    {"entity_name": "门锁", "entity_type": "设备", "entity_description": ""}
                ],
                "relations": [],
            }
            entities, relations = extractor._build_assertions(  # noqa: SLF001
                chunk=kwargs["chunk"], payload=payload
            )
            return ExtractionAttempt(
                entities=entities,
                relations=relations,
                finish_reason="stop",
                parse_result=ExtractionParseResult(
                    payload=payload,
                    parse_mode="json",
                    raw_content="{}",
                    parse_success=True,
                ),
            )
        payload = {
            "entities": [
                {"entity_name": "门锁", "entity_type": "设备", "entity_description": "门锁组件"}
            ],
            "relations": [],
        }
        entities, relations = extractor._build_assertions(  # noqa: SLF001
            chunk=kwargs["chunk"], payload=payload
        )
        return ExtractionAttempt(
            entities=entities,
            relations=relations,
            finish_reason="stop",
            parse_result=ExtractionParseResult(
                payload=payload,
                parse_mode="json",
                raw_content="{}",
                parse_success=True,
            ),
        )

    monkeypatch.setattr(extractor, "_extract_once", fake_extract_once)

    entities, relations = await extractor.extract_chunk(
        chunk=_demo_chunk(),
        modality="document",
        title="demo",
    )

    assert calls == 2
    assert len(entities) == 1
    assert entities[0].entity_description == "门锁组件"
    assert relations == []


def test_service_uses_compact_only_for_fast_ingest_mode() -> None:
    service = SingleLayerKGService(HyperRAGConfig())

    standard_extractor = service._get_extractor(extraction_mode="standard")
    precompile_extractor = service._get_extractor(extraction_mode="pre_compile")
    fast_ingest_extractor = service._get_extractor(extraction_mode="fast_ingest")

    assert standard_extractor.prompt_mode == "full"
    assert precompile_extractor.prompt_mode == "full"
    assert fast_ingest_extractor.prompt_mode == "compact"


@pytest.mark.asyncio
async def test_llm_extraction_preserves_successful_chunks_when_one_chunk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SingleLayerKGService(HyperRAGConfig())

    class FakeExtractor:
        async def extract_chunk(self, *, chunk, modality, title):
            if chunk.chunk_id == "chunk-failed":
                raise RuntimeError("missing entity_description after retries")
            return [], []

    failed_chunk = _demo_chunk()
    failed_chunk.chunk_id = "chunk-failed"
    monkeypatch.setattr(service, "_get_extractor", lambda **_kwargs: FakeExtractor())

    entities, relations, failures = await service._extract_entities_relations_via_llm(
        chunks=[_demo_chunk(), failed_chunk],
        modality="document",
        title="demo",
    )

    assert entities == []
    assert relations == []
    assert failures == [
        {
            "chunk_id": "chunk-failed",
            "error": "missing entity_description after retries",
        }
    ]


def test_parse_json_recovers_from_python_dict_style() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=192,
        entity_description_max_chars=48,
    )
    payload = "{'entities':[{'name':'门锁'}],'relations':[{'s':'门锁','p':'连接','o':'门机'}]}"

    parsed = extractor._parse_json(payload)  # noqa: SLF001

    assert isinstance(parsed, dict)
    assert "entities" in parsed
    assert "relations" in parsed


def test_parse_json_recovers_from_key_value_text_blocks() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
        max_output_tokens=192,
        entity_description_max_chars=48,
    )
    malformed = (
        'prefix {"source":"门锁","target":"门机","p":"连接","desc":"检修阶段"} '
        'suffix {"name":"门锁","type":"设备","desc":"门锁组件"}'
    )

    parsed = extractor._parse_json(malformed)  # noqa: SLF001
    normalized = extractor._normalize_payload_schema(parsed)  # noqa: SLF001

    assert isinstance(normalized, dict)
    assert len(normalized.get("relations", [])) >= 1
    assert any(item.get("source_entity") == "门锁" for item in normalized.get("relations", []))


def test_compact_caps_are_enforced_after_provider_response() -> None:
    payload = {
        "entities": [{"name": f"e-{index}"} for index in range(8)],
        "relations": [
            {"src": f"s-{index}", "tgt": f"t-{index}", "description": "连接"} for index in range(9)
        ],
    }

    capped, violated = SingleLayerKGExtractor._enforce_compact_caps(payload)  # noqa: SLF001

    assert violated is True
    assert len(capped["entities"]) == COMPACT_ER_MAX_ENTITIES
    assert len(capped["relations"]) == COMPACT_ER_MAX_RELATIONS


def test_attempt_diagnostics_distinguish_truncation_repair_recovery_and_caps() -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )

    extractor._record_attempt_diagnostics(  # noqa: SLF001
        finish_reason="length",
        parse_result=ExtractionParseResult(
            payload={"relations": []},
            parse_mode="failed",
            raw_content='{"relations":[',
            parse_success=False,
        ),
        cap_violation=False,
    )
    extractor._record_attempt_diagnostics(  # noqa: SLF001
        finish_reason="stop",
        parse_result=ExtractionParseResult(
            payload={"relations": []},
            parse_mode="repaired_json",
            raw_content='{"relations":[]',
            parse_success=True,
            repair_used=True,
        ),
        cap_violation=False,
    )
    extractor._record_attempt_diagnostics(  # noqa: SLF001
        finish_reason="stop",
        parse_result=ExtractionParseResult(
            payload={"relations": [{"src": "a", "tgt": "b"}]},
            parse_mode="text_recovery",
            raw_content='"src":"a","tgt":"b"',
            parse_success=False,
            recovery_used=True,
        ),
        cap_violation=True,
    )

    diagnostics = extractor.diagnostics_snapshot()
    assert diagnostics["model_calls"] == 3
    assert diagnostics["finish_reason_distribution"] == {"length": 1, "stop": 2}
    assert diagnostics["truncated_response_count"] == 3
    assert diagnostics["json_repair_count"] == 1
    assert diagnostics["text_recovery_count"] == 1
    assert diagnostics["schema_cap_violation_count"] == 1


@pytest.mark.asyncio
async def test_truncated_response_splits_and_merges_without_final_cap(monkeypatch) -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )
    chunk = ChunkRecord(
        chunk_id="chunk-long",
        file_path="/tmp/demo.md",
        order=0,
        raw_content=("第一段包含多个设备关系。" * 30) + "\n\n" + ("第二段包含其他参数关系。" * 30),
        text=("第一段包含多个设备关系。" * 30) + "\n\n" + ("第二段包含其他参数关系。" * 30),
        tokens=600,
        segment_ids=["seg-1"],
    )

    async def fake_extract_once(*, chunk, **kwargs):
        part = int(chunk.metadata.get("er_split_part") or 0)
        if part == 0:
            return ExtractionAttempt(
                entities=[],
                relations=[],
                finish_reason="length",
                parse_result=ExtractionParseResult(
                    payload={"entities": [], "relations": []},
                    parse_mode="failed",
                    raw_content='{"relations":[',
                    parse_success=False,
                ),
            )
        payload = {
            "entities": [],
            "relations": [
                {
                    "src": f"source-{part}-{index}",
                    "tgt": f"target-{part}-{index}",
                    "description": "连接",
                    "context": "",
                }
                for index in range(4)
            ],
        }
        entities, relations = extractor._build_assertions(chunk=chunk, payload=payload)  # noqa: SLF001
        return ExtractionAttempt(
            entities=entities,
            relations=relations,
            finish_reason="stop",
            parse_result=ExtractionParseResult(
                payload=payload,
                parse_mode="json",
                raw_content="{}",
                parse_success=True,
            ),
        )

    monkeypatch.setattr(extractor, "_extract_once", fake_extract_once)

    entities, relations = await extractor.extract_chunk(
        chunk=chunk,
        modality="document",
        title="demo",
    )

    assert len(relations) == 8
    assert len(entities) == 16
    diagnostics = extractor.diagnostics_snapshot()
    assert diagnostics["split_retry_chunk_count"] == 1
    assert diagnostics["recovered_relation_count"] == 8


@pytest.mark.asyncio
async def test_valid_empty_response_does_not_split(monkeypatch) -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )
    calls = 0

    async def fake_extract_once(**kwargs):
        nonlocal calls
        calls += 1
        return ExtractionAttempt(
            entities=[],
            relations=[],
            finish_reason="stop",
            parse_result=ExtractionParseResult(
                payload={"relations": [], "entities": []},
                parse_mode="json",
                raw_content='{"relations":[],"entities":[]}',
                parse_success=True,
            ),
        )

    monkeypatch.setattr(extractor, "_extract_once", fake_extract_once)

    entities, relations = await extractor.extract_chunk(
        chunk=_demo_chunk(),
        modality="document",
        title="demo",
    )

    assert entities == []
    assert relations == []
    assert calls == 1


@pytest.mark.asyncio
async def test_truncated_short_chunk_fails_instead_of_using_partial_output(monkeypatch) -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )

    async def fake_extract_once(**kwargs):
        return ExtractionAttempt(
            entities=[],
            relations=[],
            finish_reason="length",
            parse_result=ExtractionParseResult(
                payload={"relations": []},
                parse_mode="failed",
                raw_content='{"relations":[',
                parse_success=False,
            ),
        )

    monkeypatch.setattr(extractor, "_extract_once", fake_extract_once)

    with pytest.raises(RuntimeError, match="too short to split safely"):
        await extractor.extract_chunk(
            chunk=_demo_chunk(),
            modality="document",
            title="demo",
        )


@pytest.mark.asyncio
async def test_truncation_fails_after_maximum_split_depth(monkeypatch) -> None:
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )
    text = "密集规格参数描述。" * 400
    chunk = ChunkRecord(
        chunk_id="chunk-always-truncated",
        file_path="/tmp/demo.md",
        order=0,
        raw_content=text,
        text=text,
        tokens=2000,
        segment_ids=["seg-1"],
    )

    async def fake_extract_once(**kwargs):
        return ExtractionAttempt(
            entities=[],
            relations=[],
            finish_reason="length",
            parse_result=ExtractionParseResult(
                payload={"relations": []},
                parse_mode="failed",
                raw_content='{"relations":[',
                parse_success=False,
            ),
        )

    monkeypatch.setattr(extractor, "_extract_once", fake_extract_once)

    with pytest.raises(RuntimeError, match="remained truncated after 3 split retries"):
        await extractor.extract_chunk(
            chunk=chunk,
            modality="document",
            title="demo",
        )


@pytest.mark.asyncio
async def test_complete_text_records_input_output_and_usage(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hyperrag.observability import index_usage

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  回答内容  "))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
    )
    create = AsyncMock(return_value=response)
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
    )
    extractor._client = SimpleNamespace(  # noqa: SLF001
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    recorded: list[dict] = []
    monkeypatch.setattr(index_usage, "record_generation", lambda **kwargs: recorded.append(kwargs))

    result = await extractor.complete_text("用户问题", system_prompt="系统指令")

    assert result == "回答内容"
    assert recorded == [
        {
            "name": "kg_llm",
            "model": "demo",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            "metadata": {"phase": "general"},
            "input": [
                {"role": "system", "content": "系统指令"},
                {"role": "user", "content": "用户问题"},
            ],
            "output": "回答内容",
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_complete_text_records_failure_without_fake_usage(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hyperrag.observability import index_usage

    create = AsyncMock(side_effect=TimeoutError("provider timed out"))
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
    )
    extractor._client = SimpleNamespace(  # noqa: SLF001
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    recorded: list[dict] = []
    monkeypatch.setattr(index_usage, "record_generation", lambda **kwargs: recorded.append(kwargs))

    with pytest.raises(TimeoutError, match="provider timed out"):
        await extractor.complete_text("用户问题")

    assert len(recorded) == 1
    assert recorded[0]["name"] == "kg_llm"
    assert recorded[0]["metadata"] == {"phase": "general"}
    assert recorded[0]["usage"] is None
    assert recorded[0]["output"] is None
    assert recorded[0]["error"] == "TimeoutError: provider timed out"
    assert recorded[0]["input"] == [{"role": "user", "content": "用户问题"}]


@pytest.mark.asyncio
async def test_er_extraction_records_prompt_and_raw_response(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hyperrag.observability import index_usage

    raw = '{"relations":[]}'
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=raw),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
    )
    create = AsyncMock(return_value=response)
    extractor = SingleLayerKGExtractor(
        model="demo",
        base_url="http://127.0.0.1:8505/v1",
        api_key="k",
        prompt_mode="compact",
    )
    extractor._client = SimpleNamespace(  # noqa: SLF001
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    recorded: list[dict] = []
    monkeypatch.setattr(index_usage, "record_generation", lambda **kwargs: recorded.append(kwargs))

    await extractor._extract_once(  # noqa: SLF001
        chunk=_demo_chunk(),
        modality="document",
        title="demo",
        prompt_mode="compact",
        max_output_tokens=128,
    )

    assert len(recorded) == 1
    assert recorded[0]["name"] == "kg_er_extract"
    assert recorded[0]["metadata"] == {"prompt_mode": "compact"}
    assert recorded[0]["input"][0]["role"] == "system"
    assert recorded[0]["input"][1]["role"] == "user"
    assert recorded[0]["output"] == raw
    assert recorded[0]["usage"]["total_tokens"] == 15
