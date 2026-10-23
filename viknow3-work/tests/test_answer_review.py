from __future__ import annotations

import unittest

from viknow3.application.agent.qa_box.answer_review import review_answer
from viknow3.application.agent.qa_box.profile import AnswerReviewConfig


class AnswerReviewTests(unittest.TestCase):
    def test_low_score_allowed_when_configured(self) -> None:
        cfg = AnswerReviewConfig(True, True, 75, 1, True)
        result = review_answer("答案", cfg, library_ids=["lib"], search_called=False, citations={})
        self.assertTrue(result.passed)
        self.assertLess(result.score, 75)


if __name__ == "__main__":
    unittest.main()
