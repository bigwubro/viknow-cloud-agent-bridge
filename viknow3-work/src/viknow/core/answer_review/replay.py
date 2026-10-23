"""Independent replay boundary for timed-out answer reviews."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from viknow.core.answer_review.domain import AnswerReviewReplayInput
from viknow.core.answer_review.reviewer import AnswerReviewer


class ConfiguredAnswerReviewReplayer:
    """Replay a captured review only with the model recorded by its source run."""

    def __init__(
        self,
        *,
        configured_model: str | None,
        reviewer_factory: Callable[[AnswerReviewReplayInput], AnswerReviewer],
    ) -> None:
        self._configured_model = configured_model
        self._reviewer_factory = reviewer_factory

    def replay(self, replay_input: AnswerReviewReplayInput) -> dict:
        if replay_input.reviewer_model != self._configured_model:
            raise ValueError(
                "recorded answer review model is not available in the current configuration"
            )
        review = self._reviewer_factory(replay_input).review(replay_input.request)
        failure_reasons = {
            review.review_trace.get("claim_support_fallback_reason"),
            review.review_trace.get("semantic_review_fallback_reason"),
        }
        if any(
            isinstance(reason, str) and (reason.endswith("_failed") or reason.endswith("_timeout"))
            for reason in failure_reasons
        ):
            raise RuntimeError("answer review replay did not complete its model review")
        return {
            "passed": review.passed,
            "retryable": review.retryable,
            "result": review.model_dump(mode="json"),
        }


class DynamicAnswerReviewReplayer:
    """Resolve the current model configuration at replay time for hot reloads."""

    def __init__(self, model_config_provider: Callable[[], Any]) -> None:
        self._model_config_provider = model_config_provider

    def replay(self, replay_input: AnswerReviewReplayInput) -> dict:
        model_config = self._model_config_provider()

        def reviewer_factory(value: AnswerReviewReplayInput) -> AnswerReviewer:
            return _build_reviewer(model_config, value)

        return ConfiguredAnswerReviewReplayer(
            configured_model=model_config.resolve_model(),
            reviewer_factory=reviewer_factory,
        ).replay(replay_input)


def _build_reviewer(model_config: Any, replay_input: AnswerReviewReplayInput) -> AnswerReviewer:
    from langchain_openai import ChatOpenAI

    from viknow.core.answer_review.llm_reviewers import LLMClaimReviewer, LLMSemanticReviewer
    from viknow.core.answer_review.reviewer import RuleBasedAnswerReviewer
    from viknow.core.config import ModelProvider

    if model_config.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported answer review provider: {model_config.provider}")
    resolved_model = model_config.require_model()
    model = ChatOpenAI(
        model=resolved_model,
        base_url=model_config.resolve_base_url(),
        api_key=model_config.require_api_key(),
        temperature=0,
        timeout=replay_input.config.timeout_seconds,
        max_retries=0,
    )
    return RuleBasedAnswerReviewer(
        pass_threshold=replay_input.config.pass_threshold,
        claim_reviewer=LLMClaimReviewer(model),
        semantic_reviewer=LLMSemanticReviewer(model),
    )


def build_answer_review_replayer(
    model_config_provider: Callable[[], Any],
) -> DynamicAnswerReviewReplayer:
    return DynamicAnswerReviewReplayer(model_config_provider)
