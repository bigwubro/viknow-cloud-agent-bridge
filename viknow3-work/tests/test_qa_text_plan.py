"""The shared knowledge-qa-v2 plan records content and findings, then final_check renders."""

from __future__ import annotations

import unittest

from viknow3.agents.qa_text_plan import apply_text_plan, empty_plan, final_check, present_skill


class QaTextPlanTests(unittest.TestCase):
    def test_review_then_final_check_renders_the_finding(self) -> None:
        plan = empty_plan()
        plan, error = apply_text_plan(plan, {"action": "start", "content": "核对视频第三秒。"})
        self.assertIsNone(error)
        plan, error = apply_text_plan(
            plan,
            {
                "action": "update",
                "content": "第三秒已核对。",
                "findings": [{"text": "画面里有一个人在走。", "claim_refs": ["[C1]"]}],
            },
        )
        self.assertIsNone(error)
        rejected, error = final_check("视频第三秒的内容是什么", plan)
        self.assertFalse(rejected["submitted"])
        plan, error = apply_text_plan(plan, {"action": "review", "content": "范围已核对，可以交卷。"})
        self.assertIsNone(error)
        payload, rendered = final_check("视频第三秒的内容是什么", plan)
        self.assertTrue(payload["submitted"])
        self.assertEqual(rendered, "答案：画面里有一个人在走。[C1]")

    def test_citation_aliases_stay_out_of_finding_text(self) -> None:
        plan, error = apply_text_plan(empty_plan(), {"action": "start", "content": "核对。"})
        self.assertIsNone(error)
        _plan, error = apply_text_plan(
            plan,
            {"action": "update", "content": "核对。", "findings": [{"text": "代码为 SC [C1]", "claim_refs": ["[C1]"]}]},
        )
        self.assertIn("must not contain citation aliases", error or "")

    def test_skill_placeholder_is_replaced_for_the_model(self) -> None:
        shown = present_skill("before {{qa_final_answer_format_examples}} after")
        self.assertNotIn("{{qa_final_answer_format_examples}}", shown)
        self.assertIn("answer-templates.md", shown)


if __name__ == "__main__":
    unittest.main()
