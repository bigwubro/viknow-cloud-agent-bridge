from hyperrag.utils.segment_tools import (
    build_caption_data_main_struct,
    build_video_chunk_display_metadata,
    build_video_segments,
)


def test_build_video_segments_includes_caption_data_in_text():
    payload = {
        "metadata": {
            "title": "SX-70",
            "sources": [
                {
                    "source_id": "SX-70",
                    "source_type": "video",
                    "source_url": "/data/SX-70.mp4",
                }
            ],
        },
        "segments": [
            {
                "segment_id": "SX-70_segment_0001",
                "video_id": "SX-70",
                "time_range": [120, 150],
                "data": {
                    "caption": "最终caption说明 SX-70 的光线经反射镜折返。",
                    "segment_background": "此时是在讲解相机内部光路。",
                    "objects": ["反射镜", "镜头"],
                    "actions": ["画面-展示-光路结构-完成讲解"],
                    "state_changes": ["光路示意图出现"],
                    "ocr_keywords": ["SX-70"],
                    "technical_segment_id": "segment-2",
                    "slide_id": "slide_0001",
                    "slide_start": 0.0,
                    "slide_end": 150.0,
                },
            }
        ],
        "instructions": [
            {
                "step_id": "step_0001",
                "content": {
                    "title": "特殊光路片段",
                    "description": "展示相机内部光路",
                },
                "action_flow": [
                    {
                        "action_id": "step_0001_action_0001",
                        "action_description": "讲解 SX-70 特殊光路",
                        "segment_ids": ["SX-70_segment_0001"],
                        "references": [],
                        "assertions": [],
                    }
                ],
            }
        ],
    }

    segments = build_video_segments(
        asset_id="SX-70",
        sop_full_data=payload,
        source_parser="sop_video",
    )

    assert len(segments) == 1
    text = segments[0]["text"]
    assert text == (
        "视频描述: 最终caption说明 SX-70 的光线经反射镜折返。\nOCR: SX-70\n主要对象: 反射镜; 镜头"
    )
    assert "片段背景" not in text
    assert "主要动作" not in text
    assert "状态变化" not in text
    assert "画面展示反射镜和镜头路径" not in text
    assert segments[0]["main_struct"] == {
        "caption": "最终caption说明 SX-70 的光线经反射镜折返。",
        "asr": {},
        "ocr": {
            "ocr_keywords": ["SX-70"],
            "ocr_text": "",
        },
        "object": ["反射镜", "镜头"],
        "focus_observations": [],
    }
    assert "original_description" not in segments[0]["extra"]
    assert segments[0]["extra"]["caption_data"]["caption"].startswith("最终caption")
    assert segments[0]["extra"]["instruction_links"] == [
        {"step_id": "step_0001", "action_id": "step_0001_action_0001", "assertions": []}
    ]
    assert "draft_caption" not in segments[0]["extra"]["caption_data"]
    assert segments[0]["extra"]["technical_segment_id"] == "segment-2"
    assert segments[0]["extra"]["slide_id"] == "slide_0001"
    assert segments[0]["extra"]["slide_start"] == 0.0
    assert segments[0]["extra"]["slide_end"] == 150.0


def test_build_video_segments_includes_new_caption_data_schema_in_text():
    payload = {
        "metadata": {
            "title": "SX-70",
            "sources": [
                {
                    "source_id": "SX-70",
                    "source_type": "video",
                    "source_url": "/data/SX-70.mp4",
                }
            ],
        },
        "segments": [
            {
                "segment_id": "SX-70_segment_0001",
                "video_id": "SX-70",
                "time_range": [0, 5],
                "data": {
                    "caption": "操作员打开设备面板。",
                    "segment_background": "正在做设备面板打开后的检查准备。",
                    "asr": {"text": "请打开面板", "segments": []},
                    "visual_text": {
                        "ocr_keywords": ["POWER"],
                        "ocr_text": "POWER ON",
                    },
                    "key_objects": [
                        {
                            "category": "设备面板",
                            "state": "关闭",
                            "words": "黄色警示标签",
                        }
                    ],
                    "actions": [
                        {
                            "subject": "操作员",
                            "verb": "打开",
                            "object": "设备面板",
                            "caption": "",
                        }
                    ],
                    "state_changes": ["面板由关闭变为打开"],
                    "uncertainty": [],
                    "meta": {
                        "video_name": "SX-70.mp4",
                        "part_number": 1,
                    },
                    "caption_metadata": {
                        "caption_status": "completed",
                        "clip_path": "/tmp/clip.mp4",
                    },
                },
            }
        ],
    }

    segments = build_video_segments(
        asset_id="SX-70",
        sop_full_data=payload,
        source_parser="video",
    )

    text = segments[0]["text"]
    assert text == (
        "视频描述: 操作员打开设备面板。\nASR: 请打开面板\nOCR: POWER ON\n主要对象: 设备面板，关闭"
    )
    assert "片段背景" not in text
    assert "主要动作" not in text
    assert "状态变化" not in text
    assert segments[0]["main_struct"] == {
        "caption": "操作员打开设备面板。",
        "asr": {
            "text": "请打开面板",
            "segments": [],
        },
        "ocr": {
            "ocr_keywords": ["POWER"],
            "ocr_text": "POWER ON",
        },
        "object": [
            {
                "category": "设备面板",
                "state": "关闭",
                "words": "黄色警示标签",
            }
        ],
        "focus_observations": [],
    }
    assert segments[0]["extra"]["caption_metadata"] == {
        "caption_status": "completed",
        "clip_path": "/tmp/clip.mp4",
    }


def test_main_struct_does_not_promote_draft_caption() -> None:
    main_struct = build_caption_data_main_struct(
        {
            "draft_caption": "草稿描述",
            "video_clip_caption": "旧描述",
            "asr": {"text": "保留语音"},
        }
    )

    assert main_struct["caption"] == ""
    assert main_struct["asr"] == {"text": "保留语音"}


def test_objects_are_preserved_and_preferred_over_key_objects() -> None:
    objects = [
        {
            "name": "panel",
            "appearance": "metal",
            "role": "target",
            "position": "center",
            "description": "metal panel",
        }
    ]
    key_objects = [{"category": "panel", "state": "closed", "words": "warning"}]
    payload = {
        "metadata": {},
        "segments": [
            {
                "segment_id": "segment-1",
                "video_id": "video-1",
                "time_range": [0, 5],
                "data": {
                    "caption": "Open the panel.",
                    "objects": objects,
                    "key_objects": key_objects,
                },
            }
        ],
    }

    result = build_video_segments(
        asset_id="video-1",
        sop_full_data=payload,
        source_parser="video",
    )[0]

    assert result["main_struct"]["object"] == objects
    assert result["extra"]["caption_data"]["objects"] == objects
    assert result["extra"]["caption_data"]["key_objects"] == key_objects


def test_build_video_chunk_display_metadata_uses_main_struct_object() -> None:
    result = build_video_chunk_display_metadata(
        {
            "caption": "Open the panel.",
            "asr": {"text": "Please open the panel."},
            "ocr": {"ocr_keywords": ["POWER"], "ocr_text": "POWER OFF"},
            "object": [
                {
                    "name": "panel",
                    "description": "metal panel",
                }
            ],
        }
    )

    assert result == {
        "caption": "Open the panel.",
        "object": "panel\uff0cmetal panel",
        "ocr": "POWER OFF",
        "asr": "Please open the panel.",
    }


def test_only_observed_focus_values_enter_index_text_and_main_struct() -> None:
    focus = [
        {
            "description": "主队比分",
            "type": "int",
            "observed": True,
            "start_value": 72,
            "end_value": 74,
            "changed": True,
            "evidence": "记分牌前后两次可见",
        },
        {
            "description": "客队比分",
            "type": "int",
            "observed": True,
            "start_value": 70,
            "end_value": None,
            "changed": None,
            "evidence": "记分牌单次可见",
        },
        {
            "description": "比赛节次",
            "type": "choice",
            "observed": False,
            "start_value": None,
            "end_value": None,
            "changed": None,
            "evidence": "",
        },
    ]
    payload = {
        "metadata": {},
        "segments": [
            {
                "segment_id": "segment-1",
                "video_id": "basketball",
                "time_range": [0, 5],
                "data": {"caption": "记分牌显示比分变化。", "focus_observations": focus},
            }
        ],
    }

    result = build_video_segments(
        asset_id="basketball", sop_full_data=payload, source_parser="video"
    )[0]

    assert result["text"] == (
        "视频描述: 记分牌显示比分变化。\n"
        "关注状态: 主队比分 72→74；客队比分 70"
    )
    assert result["main_struct"]["focus_observations"] == focus
    assert "比赛节次" not in result["text"]


def test_sop_video_segments_require_explicit_segment_ids() -> None:
    payload = {
        "metadata": {},
        "segments": [
            {
                "segment_id": "segment-1",
                "video_id": "video-1",
                "time_range": [0, 5],
                "data": {"caption": "Open the panel."},
            }
        ],
        "instructions": [
            {
                "step_id": "step-1",
                "action_flow": [{"action_id": "action-1", "references": []}],
            }
        ],
    }

    import pytest

    with pytest.raises(ValueError, match="segment_ids"):
        build_video_segments(
            asset_id="video-1",
            sop_full_data=payload,
            source_parser="sop_video",
        )
