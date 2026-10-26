"""Search hits must keep the document asset id the inspect gate checks."""

from agents.runtime.agent_adapters.deepagents import _RunState, _drafted_answer
from agents.runtime.core.runtime.domain import AgentStatusType
from agents.runtime.core.runtime.evidence_ledger import _candidate_locators


def test_document_search_hit_keeps_full_doc_id_and_file_part_asset_id():
    locators = _candidate_locators(
        {
            "library_id": "e2e-artifacts-20261022",
            "evidence": [
                {
                    "id": "chunk-8dca609df47870528caaa5264380d5e1",
                    "title": "a9bdb3a4-558b-43af-a6e8-7545c40d1db3",
                    "metadata": {
                        "full_doc_id": "a9bdb3a4-558b-43af-a6e8-7545c40d1db3",
                        "chunk_id": "chunk-8dca609df47870528caaa5264380d5e1",
                    },
                    "parts": [
                        {"kind": "text", "text": "ViKnow 索引验收", "metadata": {}},
                        {
                            "kind": "file",
                            "metadata": {"asset_id": "a9bdb3a4-558b-43af-a6e8-7545c40d1db3"},
                        },
                    ],
                }
            ],
        }
    )

    assert locators
    assert {locator.asset_id for locator in locators} == {
        "a9bdb3a4-558b-43af-a6e8-7545c40d1db3"
    }
    assert {locator.segment_id for locator in locators} == {
        "chunk-8dca609df47870528caaa5264380d5e1"
    }


def test_recursion_fallback_keeps_the_longest_answer_not_a_search_plan():
    state = _RunState(
        run_id="run",
        session_id="session",
        thread_id="session",
        status=AgentStatusType.RUNNING,
        answer_candidates=[
            "Answer: 文档验收短语通常指 **索引验收**。\nLet me search more.",
            "让我重新检索。",
            "根据知识库，标题是 Index smoke。\n让我尝试查找相关的文档片段：",
        ],
    )
    drafted = _drafted_answer(state)
    assert "索引验收" in drafted
    assert "让我" not in drafted
    assert "Let me" not in drafted
