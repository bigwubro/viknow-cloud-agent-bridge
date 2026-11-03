"""Orchestrate offline parse and chunk quality review for one indexed asset."""

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chunk_completeness import ChunkSemanticReviewer
from .data_loader import AssetReviewDataLoader, ReviewRuntime
from .models import IndexQualityReport, ReviewStatus
from .parse_retention import (
    ModelAssistedRetentionReviewer,
    build_chunk_evidence,
    build_parsed_evidence,
)
from .source_extractors import SourceInformationExtractor
from .visual_review import VisualSourceEnricher


class IndexQualityReviewer:
    def __init__(
        self,
        runtime: ReviewRuntime,
        llm_func: Any,
        *,
        vision_func: Any | None = None,
        vision_sample_ratio: float = 0.25,
        parse_concurrency: int = 16,
        chunk_concurrency: int = 64,
        vision_concurrency: int = 16,
        chunk_fallback_on_error: bool = False,
    ):
        self._loader = AssetReviewDataLoader(runtime)
        self._source_extractor = SourceInformationExtractor()
        self._visual_enricher = VisualSourceEnricher(
            vision_func,
            sample_ratio=vision_sample_ratio,
            max_concurrent=vision_concurrency,
        )
        self._parse_reviewer = ModelAssistedRetentionReviewer(
            llm_func, max_concurrent=parse_concurrency
        )
        self._chunk_reviewer = ChunkSemanticReviewer(
            llm_func,
            max_concurrent=chunk_concurrency,
            fallback_on_error=chunk_fallback_on_error,
        )

    async def review(self, *, workspace: str, asset_id: str) -> IndexQualityReport:
        data = await self._loader.load(workspace=workspace, asset_id=asset_id)
        source_path = Path(data.source_path)
        source_units = self._source_extractor.extract(source_path)
        source_units = await self._visual_enricher.enrich(source_path, source_units)
        parsed_evidence = build_parsed_evidence(data.segments)
        parsed_evidence.extend(build_chunk_evidence(data.chunks))
        parse_review, chunk_reviews = await asyncio.gather(
            self._parse_reviewer.review(source_units, parsed_evidence),
            asyncio.gather(*(self._chunk_reviewer.review(chunk) for chunk in data.chunks)),
        )

        reviewed_chunks = [review for review in chunk_reviews if review.semantic_score is not None]
        macro_score = (
            sum(review.semantic_score or 0.0 for review in reviewed_chunks) / len(reviewed_chunks)
            if reviewed_chunks
            else None
        )
        weighted_denominator = sum(review.chunk.token_count or 1 for review in reviewed_chunks)
        weighted_score = (
            sum(
                (review.semantic_score or 0.0) * (review.chunk.token_count or 1)
                for review in reviewed_chunks
            )
            / weighted_denominator
            if weighted_denominator
            else None
        )
        chunk_statuses = [review.status for review in chunk_reviews]
        status = self._overall_status(
            parse_review.status, chunk_statuses or [ReviewStatus.UNREVIEWED]
        )
        return IndexQualityReport(
            generated_at=datetime.now(UTC).isoformat(),
            workspace=workspace,
            asset_id=asset_id,
            source_path=str(source_path),
            source_sha256=self._sha256(source_path),
            status=status,
            parse_review=parse_review,
            chunk_reviews=chunk_reviews,
            chunk_macro_score=macro_score,
            chunk_token_weighted_score=weighted_score,
            warnings=data.warnings,
            review_config={
                "parse_bad_case_threshold": 0.85,
                "chunk_bad_case_threshold": 0.8,
                "parse_concurrency": self._parse_reviewer.max_concurrent,
                "chunk_concurrency": self._chunk_reviewer.max_concurrent,
                "chunk_fallback_on_error": self._chunk_reviewer.fallback_on_error,
                "vision_concurrency": self._visual_enricher.max_concurrent,
                "vision_sample_ratio": self._visual_enricher.sample_ratio,
            },
        )

    @staticmethod
    def _overall_status(
        parse_status: ReviewStatus, chunk_statuses: list[ReviewStatus]
    ) -> ReviewStatus:
        statuses = [parse_status, *chunk_statuses]
        if ReviewStatus.FAIL in statuses:
            return ReviewStatus.FAIL
        if ReviewStatus.UNREVIEWED in statuses:
            return ReviewStatus.UNREVIEWED
        if ReviewStatus.WARN in statuses:
            return ReviewStatus.WARN
        return ReviewStatus.PASS

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source_file:
            for block in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
