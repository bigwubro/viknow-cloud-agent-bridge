"""The three steps kept from ViKnow 2.0's common query agent.

query_rewriting_node, knowledge_collection_node, and generation_node become
rewrite, retrieve, and answer. Language detection, rejection scoring, segment
merging, and terminology lookup stay out of this package.
"""

from __future__ import annotations

def rewrite(question: str) -> str:
    return question.strip()


def retrieve(query: str, libraries: list[str]) -> str:
    """Search the libraries this run may read. Empty means the run is not allowed."""
    if not libraries:
        return "permission_denied"
    from viknow3.application.hyperrag.client import HYPERAG

    passages: list[str] = []
    for library_id in libraries:
        found = HYPERAG.search(library_id, query, None)
        for evidence in found.get("evidence") or []:
            for part in evidence.get("parts") or []:
                text = part.get("text")
                if text:
                    passages.append(str(text))
    if not passages:
        return "permission_denied"
    return "\n".join(passages)


def answer(question: str, passage: str | None) -> str:
    if passage is None:
        return f"收到：{question}"
    if "permission_denied" in passage:
        return "查不到。"
    return f"根据手册：{passage}"
