"""A chunk-text skill does not have to inspect before the answer can finish."""

from __future__ import annotations

import unittest

from agents.runtime.core.answer_review import AnswerReviewCitationPolicy, CitationPolicyMode
from agents.runtime.core.runtime.domain import ToolCallRecord
from agents.runtime.core.runtime.stop_gate import StopCandidateGate, StopGateInput, StopGateReasonCode


class ChunkSkillGateTests(unittest.TestCase):
    def test_search_evidence_can_finish_without_inspect(self) -> None:
        decision = StopCandidateGate().evaluate(
            StopGateInput(
                question="视频第三秒的内容是什么",
                answer="答案：画面是上色。证据 id chunk-1，标题 clip.mp4",
                citation_policy=AnswerReviewCitationPolicy(
                    mode=CitationPolicyMode.OPTIONAL,
                    reason="chunk_text_without_inspect",
                    evidence_required=False,
                ),
                resolved_citation_count=0,
                completed_tool_calls=[
                    ToolCallRecord(
                        id="call-1",
                        name="knowledge_search",
                        arguments="{}",
                        result='{"evidence": [{"id": "chunk-1"}]}',
                    )
                ],
                require_inspect=False,
            )
        )
        self.assertNotEqual(decision.reason_code, StopGateReasonCode.UNINSPECTED_KNOWLEDGE_EVIDENCE)
        self.assertTrue(decision.passed)


if __name__ == "__main__":
    unittest.main()
