import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from hyperrag.video_media import encode_video_segments, resolve_segment_video_path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_materialized_segment_contains_only_requested_time_range(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x32:r=10:d=3",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    path = resolve_segment_video_path(
        global_config={"working_dir": str(tmp_path / "storage")},
        workspace="ws",
        video_name="video",
        segment_id="segment",
        video_output_format="mp4",
        metadata={"asset_id": "asset", "file_path": str(source), "begin_ts": 1, "end_ts": 2},
    )
    assert path != str(source)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(result.stdout) == pytest.approx(1.0, abs=0.1)


@pytest.mark.parametrize("legacy_cache", ["segment", "caption"])
def test_cached_crops_match_source_version_and_time_range(tmp_path, monkeypatch, legacy_cache):
    working_dir = tmp_path / "storage"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    old_crop = (
        working_dir / "ws-1" / "video_cache" / "asset-1" / "clips" / "seg-1.mp4"
        if legacy_cache == "segment"
        else tmp_path / "caption.mp4"
    )
    old_crop.parent.mkdir(parents=True, exist_ok=True)
    old_crop.write_bytes(b"stale")
    crops = []

    def materialize(source_video_path, clip_path, begin, end):
        crops.append((source_video_path, begin, end))
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"new crop")

    monkeypatch.setattr("hyperrag.video_media._materialize_temp_clip", materialize)
    kwargs = {
        "global_config": {"working_dir": str(working_dir)},
        "workspace": "ws-1",
        "video_name": "asset-1",
        "segment_id": "seg-1",
        "video_output_format": "mp4",
        "metadata": {
            "asset_id": "asset-1",
            "file_path": str(source),
            "begin_ts": 1.0,
            "end_ts": 2.0,
            "caption_metadata": {"clip_path": str(old_crop)},
        },
    }
    first = resolve_segment_video_path(**kwargs)
    assert first != str(old_crop)
    assert resolve_segment_video_path(**kwargs) == first
    assert crops == [(source, 1, 2)]
    kwargs["metadata"].update(begin_ts=20, end_ts=30)
    second = resolve_segment_video_path(**kwargs)
    assert second != first
    assert crops[-1] == (source, 20, 30)
    source.write_bytes(b"replacement source")
    third = resolve_segment_video_path(**kwargs)
    assert third not in {first, second}
    assert len(crops) == 3


def test_resolve_segment_video_path_materializes_temp_clip(tmp_path: Path, monkeypatch) -> None:
    working_dir = tmp_path / "storage"
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source")

    def fake_materialize(
        source_video_path: Path, clip_path: Path, begin_ts: float, end_ts: float
    ) -> None:
        assert source_video_path == source_path
        assert begin_ts == 3.0
        assert end_ts == 7.5
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"temp-clip")

    monkeypatch.setattr("hyperrag.video_media._materialize_temp_clip", fake_materialize)

    resolved = resolve_segment_video_path(
        global_config={"working_dir": str(working_dir)},
        workspace="ws-1",
        video_name="asset-1",
        segment_id="seg-2",
        video_output_format="mp4",
        metadata={
            "asset_id": "asset-1",
            "file_path": str(source_path),
            "begin_ts": 3,
            "end_ts": 7.5,
        },
    )

    assert Path(resolved).parent == working_dir / "temp_clips" / "ws-1" / "asset-1"
    assert Path(resolved).read_bytes() == b"temp-clip"


def test_resolve_segment_video_path_rejects_missing_time_range(tmp_path: Path) -> None:
    working_dir = tmp_path / "storage"
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source")

    with pytest.raises(ValueError, match="time range"):
        resolve_segment_video_path(
            global_config={"working_dir": str(working_dir)},
            workspace="ws-1",
            video_name="asset-1",
            segment_id="seg-3",
            video_output_format="mp4",
            metadata={"asset_id": "asset-1", "file_path": str(source_path)},
        )


@pytest.mark.asyncio
async def test_encode_video_segments_passes_semantic_text_to_embedder() -> None:
    calls = []

    class FakeEmbedder:
        async def embed_video(self, video_path, *, task=None, text=""):
            calls.append({"video_path": video_path, "task": task, "text": text})
            return [1.0, 0.0]

    result = await encode_video_segments(
        ["clip-1.mp4", "clip-2.mp4"],
        FakeEmbedder(),
        texts=[
            "<caption>first</caption>",
            "<caption>second</caption>\n<ocr_keywords>SX-70</ocr_keywords>",
        ],
    )

    assert result.shape == (2, 2)
    assert sorted(calls, key=lambda call: call["video_path"]) == [
        {
            "video_path": "clip-1.mp4",
            "task": "video_indexing",
            "text": "<caption>first</caption>",
        },
        {
            "video_path": "clip-2.mp4",
            "task": "video_indexing",
            "text": "<caption>second</caption>\n<ocr_keywords>SX-70</ocr_keywords>",
        },
    ]


@pytest.mark.asyncio
async def test_encode_video_segments_limits_sync_embedder_concurrency() -> None:
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    class FakeEmbedder:
        def embed_video_sync(self, video_path, *, task=None, text=""):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return [float(Path(video_path).stem.split("-")[-1]), 1.0]

    result = await encode_video_segments(
        ["clip-0.mp4", "clip-1.mp4", "clip-2.mp4", "clip-3.mp4"],
        FakeEmbedder(),
        texts=[""] * 4,
        max_concurrency=2,
    )

    assert max_active == 2
    assert result[:, 0].tolist() == [0.0, 1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_encode_video_segments_updates_progress_for_completed_segments(monkeypatch) -> None:
    progress_instances = []

    class FakeProgress:
        def __init__(self, *, total, desc):
            self.total = total
            self.desc = desc
            self.updates = []
            progress_instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def update(self, amount):
            self.updates.append(amount)

    class FakeEmbedder:
        def embed_video_sync(self, video_path, *, task=None, text=""):
            return [1.0, 0.0]

    monkeypatch.setattr("hyperrag.video_media.tqdm", FakeProgress)

    await encode_video_segments(
        ["clip-1.mp4", "clip-2.mp4", "clip-3.mp4"],
        FakeEmbedder(),
        texts=[""] * 3,
        progress_description="Encoding test segments",
    )

    assert len(progress_instances) == 1
    assert progress_instances[0].total == 3
    assert progress_instances[0].desc == "Encoding test segments"
    assert progress_instances[0].updates == [1, 1, 1]
