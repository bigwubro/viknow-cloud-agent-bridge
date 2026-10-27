"""ADK inspect dispatch keeps the granted library and the original question."""

from __future__ import annotations

import unittest

from viknow3.service.agent.adk_adapter.inspect import dispatch_inspect


class _Handlers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def video_inspect(self, args: dict) -> str:
        self.calls.append(("video_inspect", args))
        return "video"

    def locate_anything(self, args: dict) -> str:
        self.calls.append(("locate_anything", args))
        return "ground"


class AdkInspectDispatchTests(unittest.TestCase):
    def test_video_inspect_keeps_the_range_and_names_the_original_question(self) -> None:
        handlers = _Handlers()
        try:
            result = dispatch_inspect(
            handlers,
            "video_inspect",
            {
                "library_id": "lib-a",
                "asset_id": "asset-1",
                "time_range": {"start_seconds": 2, "end_seconds": 4},
                "question": "第三秒有什么",
            },
            library_ids=("lib-a",),
            question="视频第三秒的内容是什么",
            user_id="user",
            workspace_id="ws",
            session_id="session",
            )
        except ModuleNotFoundError as exc:
            self.skipTest(f"vision runtime is not installed in this environment: {exc}")
        self.assertEqual(result, "video")
        name, args = handlers.calls[0]
        self.assertEqual(name, "video_inspect")
        self.assertEqual(args["time_range"]["start_seconds"], 2)
        self.assertIn("视频第三秒的内容是什么", args["question"])
        self.assertIn("第三秒有什么", args["question"])

    def test_visual_ground_uses_the_locate_handler(self) -> None:
        handlers = _Handlers()
        try:
            dispatch_inspect(
            handlers,
            "visual_ground",
            {"query": "图中有几个人？", "frame_time": 3},
            library_ids=("lib-a",),
            question="有几个人",
            user_id="user",
            workspace_id="",
            session_id="session",
            )
        except ModuleNotFoundError as exc:
            self.skipTest(f"vision runtime is not installed in this environment: {exc}")
        self.assertEqual(handlers.calls[0][0], "locate_anything")
        self.assertEqual(handlers.calls[0][1]["query"], "图中有几个人？")
        self.assertEqual(handlers.calls[0][1]["frame_time"], 3)


if __name__ == "__main__":
    unittest.main()
