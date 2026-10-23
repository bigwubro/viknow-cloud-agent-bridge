from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from hyperrag.video_parser import VideoParser, default_asr_model_func


def _stub_asr_segments() -> list[dict[str, Any]]:
    return [
        {"begin_ts": 0.0, "end_ts": 8.0, "text": "你好"},
        {"begin_ts": 8.0, "end_ts": 18.0, "text": "安装步骤"},
        {"begin_ts": 35.0, "end_ts": 45.0, "text": "注意事项"},
    ]


def _fake_ffprobe() -> dict[str, Any]:
    return {
        "format": {
            "duration": "80.366",
            "tags": {"creation_time": "2024-01-02T03:04:05Z"},
        },
        "streams": [
            {
                "codec_type": "video",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30/1",
            },
            {"codec_type": "audio"},
        ],
    }


def test_video_parser_no_vlm_generates_files(tmp_path: Path, monkeypatch) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video fixture")
    transcriptions = Mock(return_value=_stub_asr_segments())

    class FakeTranscriber:
        def __init__(self, *, asr_callable, config):
            self.config = config

        def transcribe_file(self, audio_path):
            return transcriptions(audio_path)

    class FakeClip:
        duration = 80.366

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def subclipped(self, start, end):
            assert 0 <= start < end <= self.duration
            return self

        def write_videofile(self, output_path, **kwargs):
            Path(output_path).write_bytes(b"clip fixture")

    monkeypatch.setattr("hyperrag.video_parser.OpenAI", Mock())
    monkeypatch.setattr("hyperrag.video_parser.SileroVADAudioTranscriber", FakeTranscriber)
    monkeypatch.setattr("hyperrag.video_parser.VideoFileClip", lambda _: FakeClip())
    monkeypatch.setattr(VideoParser, "_ffprobe", staticmethod(lambda _: _fake_ffprobe()))
    monkeypatch.setattr(
        VideoParser,
        "_extract_audio",
        staticmethod(lambda _, path: path.write_bytes(b"audio fixture")),
    )
    monkeypatch.setattr(
        VideoParser, "_ensure_clip_has_min_frames", staticmethod(lambda *args, **kwargs: None)
    )
    cache_root = tmp_path / ".video_cache_test"
    parser = VideoParser(asr_model_func=None, video_caption_func=None, cache_root_dir=cache_root)

    asset, segments = parser.parse_document(
        doc_id="vid1",
        video_path=str(video_path),
        parser_params={"segment_length_sec": 10, "asr_lang": "zh"},
    )

    # 基本断言
    assert asset.doc_id == "vid1"
    assert asset.modality == "video"
    assert asset.segments_count == len(segments)

    # 缓存与产物断言
    # note: keep basic cache path checks below
    video_cache_dir = Path(asset.extra.get("video_cache_dir"))
    frames_dir = Path(asset.extra.get("frames_dir"))
    transcript_file = Path(asset.extra.get("transcript_file"))
    index_file = video_cache_dir / "index.json"

    assert video_cache_dir.exists() and video_cache_dir.is_dir()
    assert frames_dir.exists() and frames_dir.is_dir()
    assert transcript_file.exists()
    assert index_file.exists()

    # transcript.json preserves the complete ASR boundary response.
    with open(transcript_file, encoding="utf-8") as f:
        transcript = json.load(f)
    assert transcript == _stub_asr_segments()

    # index.json 内容
    with open(index_file, encoding="utf-8") as f:
        index_data = json.load(f)
    assert index_data.get("doc_id") == "vid1"
    assert isinstance(index_data.get("segments"), list)

    # 段断言（无 VLM，仅包含 representative_frame）
    assert len(segments) >= 1
    for seg in segments:
        assert seg.type == "video_clip"
        # 位置元数据应为窗口边界
        assert seg.location["end_ts"] > seg.location["begin_ts"]

    global_metadata = asset.extra.get("global_metadata")
    assert isinstance(global_metadata, str)
    assert "时长:" in global_metadata
    assert "平均帧率:" in global_metadata
    assert "创建时间:" in global_metadata

    # Repeated parsing should produce the same semantic result.
    asset2, segments2 = parser.parse_document(
        doc_id="vid1",
        video_path=str(video_path),
        parser_params={"segment_length_sec": 10, "asr_lang": "zh"},
    )
    assert [seg.model_dump() for seg in segments2] == [seg.model_dump() for seg in segments]
    assert asset2.model_dump() == asset.model_dump()
    assert transcriptions.call_count == 2


def test_default_asr_model_func_transcribe_basic(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio-test.mp3"
    audio_path.write_bytes(b"fake-audio")

    transcriptions = Mock()
    transcriptions.create.return_value = Mock(
        segments=[
            Mock(start=0.0, end=1.0, text="安装赤道仪"),
            Mock(start=1.0, end=2.0, text="检查刻度"),
        ]
    )
    client = Mock(audio=Mock(transcriptions=transcriptions))

    out = default_asr_model_func(
        client,
        str(audio_path),
        lang="zh",
        hotwords=["安装", "赤道仪"],
    )

    transcriptions.create.assert_called_once()
    assert transcriptions.create.call_args.kwargs["language"] == "zh"
    assert transcriptions.create.call_args.kwargs["prompt"] == "需要注意以下术语:\n安装, 赤道仪"

    # 基础结构断言（真实模型）
    assert isinstance(out, list) and len(out) >= 1
    for item in out:
        assert {"begin_ts", "end_ts", "text"}.issubset(item.keys())
        assert float(item["end_ts"]) > float(item["begin_ts"]) >= 0.0
        assert isinstance(item["text"], str)
        if "words" in item and isinstance(item["words"], list):
            for w in item["words"]:
                assert {"begin_ts", "end_ts", "text"}.issubset(w.keys())


def test_video_parser_passes_hotwords_to_asr(tmp_path: Path, monkeypatch) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_text("video", encoding="utf-8")

    class _FakeAudioTranscriber:
        def __init__(self, *, asr_callable, config):
            self.asr_callable = asr_callable
            self.config = config

        def transcribe_file(self, audio_path: str | Path):
            return self.asr_callable(
                str(audio_path),
                self.config.asr_lang,
                self.config.asr_hotwords,
            )

    monkeypatch.setattr(
        "hyperrag.video_parser.SileroVADAudioTranscriber",
        _FakeAudioTranscriber,
    )

    cache_root = tmp_path / ".video_cache_test"
    parser = VideoParser(
        asr_model_func={"base_url": "http://example.com", "api_key": "test"},
        video_caption_func=None,
        cache_root_dir=cache_root,
    )

    seen: dict[str, Any] = {}

    def fake_asr(_client, audio_file: str, lang: str = "zh", hotwords: list[str] | None = None):
        seen["audio_file"] = audio_file
        seen["lang"] = lang
        seen["hotwords"] = hotwords
        return [{"begin_ts": 0.0, "end_ts": 5.0, "text": "安装步骤"}]

    class _FakeClip:
        duration = 80.366

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def subclipped(self, _start: float, _end: float):
            return self

        def write_videofile(self, output_path: str, codec: str = "libx264", logger=None, **kwargs):
            Path(output_path).write_text("clip", encoding="utf-8")

    monkeypatch.setattr(parser, "asr_model_func", fake_asr)
    monkeypatch.setattr(VideoParser, "_calculate_file_hash", staticmethod(lambda _: "sha256:test"))
    monkeypatch.setattr(VideoParser, "_ffprobe", staticmethod(lambda _: _fake_ffprobe()))
    monkeypatch.setattr(
        VideoParser,
        "_extract_audio",
        staticmethod(
            lambda _video_path, audio_path: audio_path.write_text("audio", encoding="utf-8")
        ),
    )
    monkeypatch.setattr("hyperrag.video_parser.VideoFileClip", lambda _path: _FakeClip())

    asset, _ = parser.parse_document(
        doc_id="vid1",
        video_path=str(video_path),
        parser_params={
            "segment_length_sec": 10,
            "asr_lang": "zh",
            "asr_hotwords": ["安装", "赤道仪"],
        },
    )

    assert seen["lang"] == "zh"
    assert seen["hotwords"] == ["安装", "赤道仪"]
    assert seen["audio_file"].endswith("audio.wav")
    assert (
        asset.extra.get("global_metadata")
        == "时长:80秒；平均帧率:30fps；创建时间:2024-01-02T03:04:05Z"
    )


def test_cross_window_sentence_bridge_is_added_to_both_segments() -> None:
    raw_asr_segments = [
        {"begin_ts": 25.0, "end_ts": 29.8, "text": "请先按下急停按钮，然后"},
        {"begin_ts": 30.2, "end_ts": 33.0, "text": "确认指示灯变为红色"},
    ]
    windows = [(20.0, 30.0), (30.0, 40.0)]

    base_texts = [
        VideoParser._collect_texts_for_window(raw_asr_segments, 20.0, 30.0),
        VideoParser._collect_texts_for_window(raw_asr_segments, 30.0, 40.0),
    ]
    bridged = VideoParser._apply_cross_window_sentence_bridges(
        raw_asr_segments,
        windows,
        base_texts,
    )

    merged_sentence = "请先按下急停按钮，然后确认指示灯变为红色"
    assert merged_sentence in bridged[0]
    assert merged_sentence in bridged[1]
