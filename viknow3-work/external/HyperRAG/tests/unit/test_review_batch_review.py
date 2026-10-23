from pathlib import Path
import runpy
import sys

import pytest

from hyperrag.review.batch_review import load_batch_review_items, render_batch_summary
from hyperrag.review.models import (
    ChunkBoundaryAnalysis,
    ChunkCompletenessReview,
    IndexQualityReport,
    NormalizedChunk,
    ParseRetentionReview,
    RetentionFinding,
    ReviewStatus,
    RuleFinding,
    SemanticCompletenessJudgment,
    SourceInformationUnit,
)


def test_load_batch_review_items_requires_doc_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text("workspace,asset_id\ndefault,abc\n", encoding="utf-8")

    with pytest.raises(ValueError, match="doc_id column"):
        load_batch_review_items(csv_path, default_workspace="default")


def test_render_batch_summary_includes_metrics() -> None:
    report = IndexQualityReport(
        generated_at="2026-07-24T00:00:00Z",
        workspace="default",
        asset_id="asset-1",
        source_path="/tmp/demo.md",
        source_sha256="deadbeef",
        status=ReviewStatus.WARN,
        parse_review=ParseRetentionReview(
            status=ReviewStatus.WARN,
            information_retention=0.8,
            reviewed_weight=1.0,
            unreviewed_weight=0.0,
            findings=[
                RetentionFinding(
                    source_unit=SourceInformationUnit(
                        unit_id="u1", kind="text", content="abc"
                    ),
                    status=ReviewStatus.WARN,
                    score=0.8,
                    reason="语义缺失",
                )
            ],
        ),
        chunk_reviews=[
            ChunkCompletenessReview(
                chunk=NormalizedChunk(
                    chunk_id="c1",
                    asset_id="asset-1",
                    order=0,
                    content="abc",
                    raw_content="abc",
                ),
                status=ReviewStatus.FAIL,
                semantic_score=0.4,
                boundary_analysis=ChunkBoundaryAnalysis(
                    chunk_id="c1",
                    status=ReviewStatus.FAIL,
                    heuristic_score=0.4,
                    findings=[
                        RuleFinding(
                            code="cut",
                            severity=ReviewStatus.FAIL,
                            message="cut",
                        )
                    ],
                ),
                judgment=SemanticCompletenessJudgment(
                    semantic_completeness=0.4,
                    standalone_understandability=0.4,
                    boundary_integrity=0.4,
                    missing_context_types=[],
                    bad_span=None,
                    reason="边界截断",
                ),
            )
        ],
        chunk_macro_score=0.4,
        chunk_token_weighted_score=0.4,
        warnings=[],
        review_config={},
    )

    markdown = render_batch_summary([report], source_csv=Path("assets.csv"))

    assert "索引质量审查批量汇总" in markdown
    assert "asset-1" in markdown
    assert "0.800" in markdown
    assert "0.400" in markdown
    assert "## 4. 附录：逐文档 bad case 详情" in markdown
    assert "doc_id: asset-1" in markdown


def test_parse_args_allows_csv_without_asset_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Load the repository script, not an unrelated installed "scripts" package.
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "review_index_quality.py"
    parse_args = runpy.run_path(str(script_path))["parse_args"]

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_index_quality.py",
            "--config",
            "/tmp/hyperrag.yml",
            "--workspace",
            "default",
            "--csv",
            "/tmp/assets.csv",
        ],
    )

    args = parse_args()

    assert args.csv == "/tmp/assets.csv"
    assert args.asset_id is None
    assert args.judge_timeout == 25.0
    assert args.judge_max_retries == 0
    assert args.parse_concurrency == 8
    assert args.chunk_concurrency == 16
    assert args.vision_concurrency == 8
    assert args.vision_sample_ratio == 0.25
    assert args.chunk_fallback_on_error is True
