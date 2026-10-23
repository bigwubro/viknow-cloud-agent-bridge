"""Demo agent profiles used by the local API app."""

from __future__ import annotations

from viknow.core.runtime.domain import AgentProfile

TEST_QA_SYSTEM_PROMPT = (
    "You are ViKnow's minimal QA agent. For every user question, "
    "first call the viknow_test_answer tool, then answer using the "
    "tool result."
)


def knowledge_qa_profile() -> AgentProfile:
    return AgentProfile(
        id="knowledge",
        label="Knowledge QA",
        description="Knowledge and multimodal question answering",
        enabled_tools=[
            "knowledge_discover",
            "knowledge_search",
            "video_inspect",
            "evidence_asr",
            "evidence_ocr",
            "clip_search",
            "global_browse",
            "frame_inspect",
        ],
        enabled_skills=["knowledge-qa"],
        user_prompt_template="Question: {input}",
    )


def test_qa_profile() -> AgentProfile:
    return AgentProfile(
        id="test",
        label="Test QA",
        description="Minimal test question answering",
        enabled_tools=["viknow_test_answer"],
        system_prompt=TEST_QA_SYSTEM_PROMPT,
        user_prompt_template="Question: {input}",
    )
