from pathlib import Path

from hyperrag.review.models import NormalizedChunk, ReviewStatus, SourceInformationUnit
from hyperrag.review.parse_retention import (
    DeterministicRetentionReviewer,
    ModelAssistedRetentionReviewer,
    build_chunk_evidence,
    build_parsed_evidence,
)
from hyperrag.review.source_extractors import SourceInformationExtractor
from openpyxl import Workbook


def test_markdown_extractor_preserves_block_kinds(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text(
        "# Maintenance\n\n"
        "1. Cut power.\n2. Lock the switch.\n\n"
        "| Item | State |\n| --- | --- |\n| Brake | Closed |",
        encoding="utf-8",
    )

    units = SourceInformationExtractor().extract(source)

    assert [unit.kind for unit in units] == ["heading", "list", "table"]


def test_xlsx_extractor_keeps_formula_as_source_information(tmp_path: Path) -> None:
    source = tmp_path / "formula.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Load"
    sheet["B1"] = "=SUM(B2:B8)"
    workbook.save(source)

    units = SourceInformationExtractor().extract(source)

    formula = next(unit for unit in units if unit.kind == "formula")
    assert formula.content == "=SUM(B2:B8)"
    assert formula.weight == 1.5


def test_retention_uses_source_recall_and_reports_missing_unit() -> None:
    source_units = [
        SourceInformationUnit(unit_id="one", kind="text", content="切断主电源并锁定开关。"),
        SourceInformationUnit(unit_id="two", kind="text", content="确认制动器处于闭合状态。"),
    ]
    evidence = build_parsed_evidence(
        [
            {
                "segment_id": "seg-1",
                "type": "text",
                "text": "维护前切断主电源，并锁定开关。",
                "location": {"page": 0},
                "extra": {},
            }
        ]
    )

    review = DeterministicRetentionReviewer().review(source_units, evidence)

    assert review.status == ReviewStatus.FAIL
    assert review.information_retention is not None
    assert 0.3 < review.information_retention < 0.6
    assert review.findings[1].status == ReviewStatus.FAIL


def test_generated_visual_caption_does_not_raise_deterministic_retention() -> None:
    source_units = [
        SourceInformationUnit(unit_id="one", kind="text", content="原始文件包含紧急制动步骤。")
    ]
    evidence = build_parsed_evidence(
        [
            {
                "segment_id": "generated",
                "type": "image",
                "text": "[视觉要点] 原始文件包含紧急制动步骤。",
                "location": {"page": 0},
                "extra": {"source": "image_enhancer"},
            }
        ]
    )

    review = DeterministicRetentionReviewer().review(source_units, evidence)

    assert review.information_retention == 0.0
    assert review.status == ReviewStatus.FAIL


def test_localized_chunk_evidence_can_recover_pdf_table_retention() -> None:
    source_units = [
        SourceInformationUnit(
            unit_id="page-16",
            kind="page_text",
            locator={"page": 16},
            content="术语 DRAM SDRAM DDR LPDDR DIMM SPD ODT SI PI",
        )
    ]
    segment_evidence = build_parsed_evidence(
        [
            {
                "segment_id": "seg-0182",
                "type": "table",
                "text": "附录 A 术语速查",
                "location": {"page": 16},
                "extra": {},
            }
        ]
    )
    chunk_evidence = build_chunk_evidence(
        [
            NormalizedChunk(
                chunk_id="chunk-ad78583",
                asset_id="asset-1",
                order=0,
                content=(
                    "| 术语 | 中文/解释 |\n"
                    "| DRAM | 动态随机存取存储器 |\n"
                    "| SDRAM | 与外部时钟同步工作的 DRAM |\n"
                    "| DDR | SDRAM 接口家族 |\n"
                    "| LPDDR | 低功耗 DRAM 家族 |\n"
                    "| DIMM | 双列直插内存模组 |\n"
                    "| SPD | 串行存在检测信息 |\n"
                    "| ODT | 片上终端电阻 |\n"
                    "| SI / PI | 信号完整性 / 电源完整性 |"
                ),
                raw_content="",
                metadata={
                    "chunk_type": "image",
                    "locations": [{"page": 16, "bbox": [0, 0, 100, 100]}],
                },
            )
        ]
    )

    review_without_chunk = DeterministicRetentionReviewer().review(source_units, segment_evidence)
    review_with_chunk = DeterministicRetentionReviewer().review(
        source_units, [*segment_evidence, *chunk_evidence]
    )

    assert review_without_chunk.information_retention is not None
    assert review_without_chunk.information_retention < 0.5
    assert review_with_chunk.information_retention is not None
    assert review_with_chunk.information_retention >= 0.8
    assert review_with_chunk.findings[0].matched_segment_ids == ["chunk:chunk-ad78583"]


def test_unreviewable_visual_relation_is_excluded_from_denominator() -> None:
    source_units = [
        SourceInformationUnit(
            unit_id="shape",
            kind="visual_relation",
            content="2 connector semantics",
            weight=2.0,
            reviewable=False,
            risk_codes=["connector_semantics"],
        )
    ]

    review = DeterministicRetentionReviewer().review(source_units, [])

    assert review.status == ReviewStatus.UNREVIEWED
    assert review.information_retention is None
    assert review.unreviewed_weight == 2.0
    assert review.risk_codes == ["connector_semantics"]


async def test_model_failure_keeps_parse_review_unreviewed() -> None:
    async def broken_judge(_prompt: str, **_kwargs):
        raise RuntimeError("model unavailable")

    source_units = [SourceInformationUnit(unit_id="missing", kind="text", content="缺失信息。")]

    review = await ModelAssistedRetentionReviewer(broken_judge).review(source_units, [])

    assert review.status == ReviewStatus.FAIL
    assert review.findings[0].status == ReviewStatus.FAIL
    assert "Model-assisted review failed" in review.findings[0].reason
