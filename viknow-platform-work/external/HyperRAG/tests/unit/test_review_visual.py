from pathlib import Path

import pytest
from hyperrag.review.models import SourceInformationUnit
from hyperrag.review.visual_review import VisualSourceEnricher


def test_visual_sampling_prioritizes_connector_and_animation_risks() -> None:
    units = [
        SourceInformationUnit(
            unit_id="slide-1",
            kind="visual_slide",
            content="visual",
            reviewable=False,
            risk_codes=["visual_slide_requires_review"],
        ),
        SourceInformationUnit(
            unit_id="slide-2",
            kind="visual_slide",
            content="visual",
            reviewable=False,
            risk_codes=["visual_slide_requires_review", "connector_semantics"],
        ),
    ]

    sampled = VisualSourceEnricher(lambda *_args: "", sample_ratio=0.5)._sample_ids(units)

    assert sampled == {"slide-2"}


@pytest.mark.asyncio
async def test_visual_review_without_model_stays_unreviewed(tmp_path: Path) -> None:
    unit = SourceInformationUnit(
        unit_id="page-0",
        kind="visual_page",
        locator={"page": 0},
        content="visual",
        reviewable=False,
        risk_codes=["visual_page_requires_review"],
    )

    result = await VisualSourceEnricher(None).enrich(tmp_path / "missing.pdf", [unit])

    assert result[0].reviewable is False
    assert result[0].risk_codes == ["visual_page_requires_review"]
