import json

from hyperrag.review import ChunkBoundaryAnalyzer, ReviewStatus, normalize_chunk


def test_normalize_chunk_unwraps_hyperchunker_content() -> None:
    chunk = normalize_chunk(
        {
            "chunk_id": "chunk-1",
            "content": json.dumps(
                {"doc_id": "asset-1", "order": 3, "content": "完整的段落。"},
                ensure_ascii=False,
            ),
            "tokens": 8,
            "segment_ids": ["seg-1"],
            "metadata": {"edited": False},
        },
        asset_id="asset-1",
    )

    assert chunk.content == "完整的段落。"
    assert chunk.order == 3
    assert chunk.warnings == []


def test_normalize_chunk_marks_legacy_plain_text() -> None:
    chunk = normalize_chunk(
        {"chunk_id": "legacy", "content": "legacy plain text"},
        asset_id="asset-1",
    )

    assert chunk.content == "legacy plain text"
    assert chunk.warnings == ["legacy_plain_text_content"]


def test_boundary_analyzer_flags_broken_table_and_code_fence() -> None:
    chunk = normalize_chunk(
        {
            "chunk_id": "broken",
            "content": json.dumps(
                {
                    "doc_id": "asset-1",
                    "order": 0,
                    "content": "| value | orphan |\n| next | row |\n```python\nprint('x')",
                }
            ),
        },
        asset_id="asset-1",
    )

    result = ChunkBoundaryAnalyzer().analyze(chunk)

    assert result.status == ReviewStatus.FAIL
    assert {finding.code for finding in result.findings} >= {
        "table_without_header",
        "unbalanced_code_fence",
    }


def test_boundary_analyzer_accepts_complete_paragraph() -> None:
    chunk = normalize_chunk(
        {
            "chunk_id": "complete",
            "content": json.dumps(
                {
                    "doc_id": "asset-1",
                    "order": 0,
                    "content": "执行维护前，应切断主电源并确认设备已经停止运行。",
                },
                ensure_ascii=False,
            ),
        },
        asset_id="asset-1",
    )

    result = ChunkBoundaryAnalyzer().analyze(chunk)

    assert result.status == ReviewStatus.PASS
    assert result.heuristic_score == 1.0
    assert result.findings == []


def test_boundary_analyzer_reports_modal_replay_without_changing_content() -> None:
    chunk = normalize_chunk(
        {
            "chunk_id": "edited",
            "content": "A complete legacy paragraph with enough context.",
            "metadata": {"edited": True},
        },
        asset_id="asset-1",
    )

    result = ChunkBoundaryAnalyzer().analyze(chunk)

    assert result.status == ReviewStatus.WARN
    assert [finding.code for finding in result.findings] == ["modal_replay_content"]
