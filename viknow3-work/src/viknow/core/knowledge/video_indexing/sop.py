"""SOP parsing and alignment for video index extraction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.knowledge.video_indexing.domain import SegmentDescription, SopAlignment, SopStep
from viknow.core.knowledge.video_indexing.index_support import cosine_similarity


class SopContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[SopStep] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)
    summary: str = ""


class SopContextExtractor:
    """Extract numbered SOP steps and a compact business context."""

    def extract(self, sop_path: Path, *, document_uri: str | None = None) -> SopContext:
        lines = sop_path.read_text(encoding="utf-8").splitlines()
        steps: list[SopStep] = []
        counters = {"In": 0, "Out": 0}
        resolved_document_uri = document_uri or str(sop_path)
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            match = re.match(r"^(?P<number>\d+)[）)](?P<text>.+)$", line)
            if not match:
                continue
            text = match.group("text").strip().rstrip("；")
            section = "In" if line_number < 18 else "Out"
            counters[section] += 1
            steps.append(
                SopStep(
                    id=f"SOP_{section}_{counters[section]}",
                    text=text,
                    document_uri=resolved_document_uri,
                    start_line=line_number,
                    end_line=line_number,
                )
            )
        terms = sorted({term for step in steps for term in extract_terms(step.text)})
        summary = "；".join(step.text for step in steps[:6])
        return SopContext(steps=steps, terms=terms, summary=summary)


class SopStepAligner:
    """Align one slice description to SOP steps using real embeddings."""

    def __init__(self, *, embedding_client: Any, min_confidence: float = 0.55) -> None:
        self.embedding_client = embedding_client
        self.min_confidence = min_confidence

    def align(self, description: SegmentDescription, steps: list[SopStep]) -> SopAlignment:
        if not steps:
            return SopAlignment(slice_id=description.slice_id, sop_ids=[], confidence=0.0)
        query_text = "\n".join(
            [description.title, description.action, description.semantic, *description.states]
        )
        query_vector = self.embedding_client.embed({"text": query_text})
        scored = []
        for step in steps:
            step_vector = self.embedding_client.embed({"text": step.text})
            score = cosine_similarity(query_vector, step_vector)
            scored.append((score, step))
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score = scored[0][0]
        minimum_score = max(self.min_confidence, best_score - 0.08)
        selected = [step.id for score, step in scored[:2] if score >= minimum_score]
        return SopAlignment(
            slice_id=description.slice_id,
            sop_ids=selected,
            confidence=max(0.0, min(1.0, best_score)),
            review_required=best_score < self.min_confidence,
            reason="embedding similarity alignment",
        )


def extract_terms(text: str) -> list[str]:
    known_terms = [
        "阻门器",
        "层门",
        "轿顶",
        "急停",
        "照明",
        "检修",
        "轿厢",
        "围栏",
        "公共",
        "上行",
        "下行",
    ]
    return [term for term in known_terms if term in text]
