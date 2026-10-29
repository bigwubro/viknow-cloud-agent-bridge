"""Answer review gate (viknow2 answer_review subset, A4)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from viknow_platform.application.agent.knowledge_assistant.profile import AnswerReviewConfig


@dataclass(frozen=True)
class AnswerReviewResult:
    passed: bool
    score: int
    reason: str
    output: str


def review_answer(
    text: str,
    config: AnswerReviewConfig,
    *,
    library_ids: list[str],
    search_called: bool,
    citations: dict[str, Any],
) -> AnswerReviewResult:
    if not config.enabled or not config.gate_enabled:
        return AnswerReviewResult(True, 100, "disabled", text)
    stripped = (text or "").strip()
    if not stripped:
        return AnswerReviewResult(False, 0, "empty_answer", stripped)
    score = 80
    reasons: list[str] = []
    if library_ids and not search_called:
        score -= 35
        reasons.append("kb_without_search")
    if library_ids and not citations.get("items"):
        score -= 15
        reasons.append("no_citations")
    if re.search(r"\[(\d+)\]", stripped) is None and library_ids and search_called:
        score -= 10
        reasons.append("missing_citation_markers")
    passed = score >= config.pass_threshold
    reason = ",".join(reasons) if reasons else "ok"
    if passed:
        return AnswerReviewResult(True, score, reason, stripped)
    if config.allow_low_confidence_output:
        return AnswerReviewResult(True, score, f"low_confidence:{reason}", stripped)
    return AnswerReviewResult(False, score, reason, stripped)


def emit_answer_review(state, events, profile, harness_ctx) -> None:
    """Emit answer review SSE events and optionally rewrite state.output."""
    text = state.output or ""
    citations = harness_ctx.citations.export()
    state.citations = citations
    events.emit("answer_review_started", {"library_ids": list(harness_ctx.facts.library_ids)})
    result = review_answer(
        text,
        profile.answer_review,
        library_ids=list(harness_ctx.facts.library_ids),
        search_called=harness_ctx.facts.search_called,
        citations=citations,
    )
    events.emit(
        "answer_review_completed",
        {"passed": result.passed, "score": result.score, "reason": result.reason},
    )
    if result.output != text:
        state.output = result.output
