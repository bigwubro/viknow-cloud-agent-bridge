"""Video jobs must preserve external transcript input within the selected asset."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from hyperrag.task import handler


def test_video_handler_forwards_external_transcript_and_rejects_foreign_asset(tmp_path):
    transcript = {
        "schema_version": "video-transcript.v1",
        "video_id": "asset",
        "segments": [],
        "source": {"kind": "dataset"},
    }
    semantic = {
        "metadata": {
            "sources": [
                {
                    "source_id": "asset",
                    "source_type": "video",
                    "source_url": str(tmp_path / "silent.mp4"),
                }
            ]
        },
        "segments": [
            {
                "segment_id": "segment",
                "video_id": "asset",
                "time_range": [0, 10],
                "data": {"caption": "A panel demonstration", "asr": "Dataset speech without audio"},
            }
        ],
    }
    calls = []

    class FakeParser:
        def __init__(self, config):
            pass

        def parse_video_only(self, video, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(merged_sop=semantic, segment_output={})

    store = SimpleNamespace(get_video_cache_root=lambda workspace: tmp_path / workspace)
    config = {
        key: str(tmp_path / (key + ".yaml"))
        for key in ("models_config_path", "segment_config", "aggregate_config", "merge_config")
    }
    job = handler.VideoParseHandler(store, actio_config=config)
    task = SimpleNamespace(
        job_id="isolated-test",
        workspace="library",
        asset_id="asset",
        metadata={
            "asset_path": str(tmp_path / "silent.mp4"),
            "params": {"external_transcript": transcript},
        },
    )
    with patch.object(handler, "ActioVideoParser", FakeParser):
        result = asyncio.run(job(task))
        assert result.status == "completed"
        assert calls[0]["external_transcript"] == transcript
        assert "Dataset speech without audio" in result.data["content"]
        task.metadata["params"]["external_transcript"] = dict(transcript, video_id="foreign")
        failed = asyncio.run(job(task))
        assert failed.status == "failed"
        assert "another asset" in failed.error
        assert len(calls) == 1
