import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hyperrag.review.models import ReviewStatus
from hyperrag.review.reporting import render_markdown
from hyperrag.review.reviewer import IndexQualityReviewer


class FakeRuntime:
    def __init__(self, source_path: Path, *, include_chunks: bool = True):
        self.source_path = source_path
        self.include_chunks = include_chunks
        self.initialized = False

    async def initialize_readonly(self) -> None:
        self.initialized = True

    async def aget_asset_status(self, workspace: str, asset_id: str):
        return SimpleNamespace(file_path=str(self.source_path))

    async def aget_file_snapshot(self, workspace: str, file_id: str):
        return {
            "parse_mode": "document",
            "asset": {"doc_id": file_id},
            "segments": [
                {
                    "segment_id": "seg-1",
                    "type": "text",
                    "text": "切断主电源并锁定开关。",
                    "location": {"page": 0},
                    "extra": {},
                }
            ],
        }

    async def aget_asset_chunks(self, workspace: str, file_id: str, page=None):
        if not self.include_chunks:
            return []
        return [
            {
                "chunk_id": "chunk-1",
                "content": json.dumps(
                    {"doc_id": file_id, "order": 0, "content": "切断主电源并锁定开关。"},
                    ensure_ascii=False,
                ),
                "tokens": 12,
                "segment_ids": ["seg-1"],
            }
        ]


async def fake_judge(prompt: str, **_kwargs) -> str:
    if "retention_score" in prompt:
        return json.dumps(
            {
                "retention_score": 0.95,
                "matched_segment_ids": ["seg-1"],
                "reason": "原始步骤被完整保留。",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "semantic_completeness": 0.95,
            "standalone_understandability": 0.95,
            "boundary_integrity": 0.9,
            "missing_context_types": [],
            "bad_span": None,
            "reason": "语义完整。",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_reviewer_loads_asset_and_generates_bad_case_ready_report(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("切断主电源并锁定开关。", encoding="utf-8")
    runtime = FakeRuntime(source)

    report = await IndexQualityReviewer(runtime, fake_judge).review(
        workspace="kb-test", asset_id="asset-1"
    )

    assert runtime.initialized is True
    assert report.status == ReviewStatus.PASS
    assert report.parse_review.information_retention == 1.0
    assert report.chunk_macro_score == pytest.approx(0.9333333333)
    markdown = render_markdown(report)
    assert "Information retention: **1.000**" in markdown
    assert "No chunk bad cases found." in markdown


@pytest.mark.asyncio
async def test_judge_failure_is_not_reported_as_low_quality(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("切断主电源并锁定开关。", encoding="utf-8")

    async def broken_judge(_prompt: str, **_kwargs):
        raise RuntimeError("model unavailable")

    report = await IndexQualityReviewer(FakeRuntime(source), broken_judge).review(
        workspace="kb-test", asset_id="asset-1"
    )

    assert report.status == ReviewStatus.UNREVIEWED
    assert report.chunk_reviews[0].status == ReviewStatus.UNREVIEWED
    assert report.chunk_reviews[0].semantic_score is None


@pytest.mark.asyncio
async def test_missing_chunks_produces_partial_unreviewed_report(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("切断主电源并锁定开关。", encoding="utf-8")

    report = await IndexQualityReviewer(
        FakeRuntime(source, include_chunks=False), fake_judge
    ).review(workspace="kb-test", asset_id="asset-1")

    assert report.status == ReviewStatus.UNREVIEWED
    assert report.chunk_macro_score is None
    assert report.warnings == ["no_chunks_found"]


@pytest.mark.asyncio
async def test_chunk_judge_failure_with_fallback_uses_boundary_score(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("切断主电源并锁定开关。", encoding="utf-8")

    async def broken_judge(_prompt: str, **_kwargs):
        raise RuntimeError("Request timed out")

    report = await IndexQualityReviewer(
        FakeRuntime(source), broken_judge, chunk_fallback_on_error=True
    ).review(workspace="kb-test", asset_id="asset-1")

    assert report.chunk_reviews[0].status == ReviewStatus.WARN
    assert report.chunk_reviews[0].semantic_score is not None
    assert "judge_failed_fallback_to_boundary" in str(report.chunk_reviews[0].error)
