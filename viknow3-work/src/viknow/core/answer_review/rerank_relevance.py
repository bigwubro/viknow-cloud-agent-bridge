"""Rerank-backed claim relevance scoring for answer review."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from viknow.core.answer_review.domain import (
    AnswerReviewClaimRelevancePair,
    AnswerReviewClaimRelevanceScore,
)


class HyperRAGRerankClaimRelevanceScorer:
    """Score answer-span and cited-claim relevance with HyperRAG's reranker client."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        api_url: str,
        repo_root: Path | None = None,
        config_path: Path | None = None,
        mode: str = "text",
        batch_size: int = 4,
        batch_concurrency: int = 4,
        max_retries: int = 2,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.repo_root = repo_root
        self.config_path = config_path
        self.mode = mode
        self.batch_size = batch_size
        self.batch_concurrency = batch_concurrency
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds

    def score_relevance(
        self,
        pairs: list[AnswerReviewClaimRelevancePair],
    ) -> list[AnswerReviewClaimRelevanceScore]:
        if not pairs:
            return []
        return _run_rerank_sync(self._score_relevance_async(pairs))

    async def _score_relevance_async(
        self,
        pairs: list[AnswerReviewClaimRelevancePair],
    ) -> list[AnswerReviewClaimRelevanceScore]:
        reranker_cls, candidate_cls = _load_hyperrag_reranker(
            self.repo_root,
            self.config_path,
        )
        scores: list[AnswerReviewClaimRelevanceScore] = []
        for answer_span, group in _pairs_by_answer_span(pairs).items():
            reranker = reranker_cls(
                api_key=self.api_key,
                model=self.model,
                api_url=self.api_url,
                mode=self.mode,
                batch_size=self.batch_size,
                batch_concurrency=self.batch_concurrency,
                max_retries=self.max_retries,
                timeout_seconds=self.timeout_seconds,
            )
            candidates = [
                candidate_cls(
                    id=pair.id,
                    text=pair.cited_claim,
                    metadata={
                        "span_id": pair.span_id,
                        "citation_marker": pair.citation_marker,
                        "cited_claim_ref": pair.cited_claim_ref,
                    },
                )
                for pair in group
            ]
            rerank_results = await reranker.rerank(answer_span, candidates)
            for result in rerank_results:
                scores.append(
                    AnswerReviewClaimRelevanceScore(
                        pair_id=str(result.candidate_id),
                        score=float(result.relevance_score),
                    )
                )
        return scores


def _pairs_by_answer_span(
    pairs: list[AnswerReviewClaimRelevancePair],
) -> dict[str, list[AnswerReviewClaimRelevancePair]]:
    grouped: dict[str, list[AnswerReviewClaimRelevancePair]] = {}
    for pair in pairs:
        grouped.setdefault(pair.answer_span, []).append(pair)
    return grouped


def _load_hyperrag_reranker(repo_root: Path | None, config_path: Path | None):
    from viknow.core.knowledge.hyperrag_adapter import _prepare_hyperrag_import_path

    _prepare_hyperrag_import_path(
        repo_root=repo_root,
        config_path=config_path or Path.cwd(),
    )
    from hyperrag.rerank import OpenAICompatibleReranker, RerankCandidate

    return OpenAICompatibleReranker, RerankCandidate


def _run_rerank_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result = None
    error: BaseException | None = None

    def runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=runner, name="answer-review-rerank", daemon=True)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    return result


__all__ = ["HyperRAGRerankClaimRelevanceScorer"]
