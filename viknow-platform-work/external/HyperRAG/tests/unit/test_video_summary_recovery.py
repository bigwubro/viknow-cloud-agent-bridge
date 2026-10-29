"""Summary edits must not silently lose jobs or replay uncertain submissions."""

import asyncio
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import hyperrag.hyperrag_v2 as module
import pytest
from hyperrag.hyperrag_v2 import HyperRAGV2, VideoUpdateSubmissionUnknown


def setup_app(tmp_path, monkeypatch):
    path = tmp_path / "sop-full.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "title": "title",
                    "global_summary": "old",
                    "global_context": {"keywords": [], "attention_items": []},
                }
            }
        )
    )
    status = SimpleNamespace(
        parse_mode="video", status="completed", progress=1, file_path="/video.mp4", file_data=None
    )
    storage = SimpleNamespace(get_status=AsyncMock(return_value=status), set_status=AsyncMock())
    monkeypatch.setattr(module, "get_asset_status_storage", lambda: storage)
    app = object.__new__(HyperRAGV2)
    app._initialized = True
    app.scheduler = SimpleNamespace(submit_job=AsyncMock(return_value="job-1"))
    app._queue = SimpleNamespace(redis=SimpleNamespace(set=AsyncMock()))
    app._get_video_semantic_paths = Mock(return_value=(path, [path]))
    monkeypatch.setattr(
        module.JobBuilder, "from_asset", lambda **kwargs: (SimpleNamespace(job_id="job-1"), [])
    )
    return app, path, storage


@pytest.mark.asyncio
async def test_pre_submission_failure_restores_original_and_can_retry(tmp_path, monkeypatch):
    app, path, storage = setup_app(tmp_path, monkeypatch)
    original = path.read_bytes()
    app._initialized = False
    app.initialize = AsyncMock(side_effect=RuntimeError("preflight"))
    with pytest.raises(RuntimeError):
        await app.aupdate_video_global_summary("ws", "asset", "new")
    assert path.read_bytes() == original
    storage.set_status.assert_not_awaited()
    app._initialized = True
    assert await app.aupdate_video_global_summary("ws", "asset", "new") == "job-1"


@pytest.mark.asyncio
async def test_unknown_submission_keeps_job_handle_and_never_replays(tmp_path, monkeypatch):
    app, path, storage = setup_app(tmp_path, monkeypatch)
    app.scheduler.submit_job.side_effect = TimeoutError("may have been accepted")
    with pytest.raises(VideoUpdateSubmissionUnknown):
        await app.aupdate_video_global_summary("ws", "asset", "new")
    metadata = json.loads(path.read_text())["metadata"]
    assert metadata["global_summary"] == "new"
    assert metadata["global_summary_submission"] == {"status": "unknown", "job_id": "job-1"}
    with pytest.raises(ValueError, match="job-1"):
        await app.aupdate_video_global_summary("ws", "asset", "new")
    app.scheduler.submit_job.assert_awaited_once()
    assert storage.set_status.await_count == 1


@pytest.mark.asyncio
async def test_failed_status_transition_is_restored_before_any_submit(tmp_path, monkeypatch):
    app, path, storage = setup_app(tmp_path, monkeypatch)
    original = path.read_bytes()
    storage.set_status.side_effect = [RuntimeError("status write failed"), None]
    with pytest.raises(RuntimeError):
        await app.aupdate_video_global_summary("ws", "asset", "new")
    assert path.read_bytes() == original
    assert storage.set_status.await_args.args[2] == "completed"
    app.scheduler.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_recovery_never_overwrites_concurrent_content(tmp_path, monkeypatch):
    app, path, storage = setup_app(tmp_path, monkeypatch)
    concurrent = json.dumps({"metadata": {"global_summary": "other writer"}})

    async def fail(**kwargs):
        path.write_text(concurrent)
        raise ValueError("pre-submission")

    app.aupdate_video_index = fail
    with pytest.raises(ValueError):
        await app.aupdate_video_global_summary("ws", "asset", "new")
    assert path.read_text() == concurrent


@pytest.mark.asyncio
async def test_cancelled_mapping_after_submit_preserves_new_metadata(tmp_path, monkeypatch):
    app, path, storage = setup_app(tmp_path, monkeypatch)
    app._queue.redis.set.side_effect = asyncio.CancelledError()
    with pytest.raises(VideoUpdateSubmissionUnknown) as raised:
        await app.aupdate_video_global_summary("ws", "asset", "new")
    assert raised.value.job_id == "job-1"
    metadata = json.loads(path.read_text())["metadata"]
    assert metadata["global_summary"] == "new"
    assert metadata["global_summary_submission"]["job_id"] == "job-1"
    app.scheduler.submit_job.assert_awaited_once()


@pytest.mark.parametrize("mode", [0o644, 0o664])
@pytest.mark.asyncio
async def test_atomic_save_preserves_shared_file_permissions(tmp_path, monkeypatch, mode):
    app, path, storage = setup_app(tmp_path, monkeypatch)
    path.chmod(mode)
    await app.aupdate_video_global_summary("ws", "asset", "new")
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_atomic_save_inherits_legacy_source_permissions(tmp_path):
    source = tmp_path / "legacy.json"
    source.write_text("{}")
    source.chmod(0o664)
    target = tmp_path / "canonical.json"
    module._write_video_summary_text(target, "{}", source_path=source)
    assert stat.S_IMODE(target.stat().st_mode) == 0o664


@pytest.mark.parametrize("fail_submission", [False, True])
@pytest.mark.asyncio
async def test_summary_edit_promotes_legacy_without_overwriting_it(
    tmp_path, monkeypatch, fail_submission
):
    app, legacy, _storage = setup_app(tmp_path, monkeypatch)
    original = legacy.read_bytes()
    canonical = legacy.with_name("video-semantic.json")
    app._get_video_semantic_paths = Mock(return_value=(canonical, [canonical, legacy]))
    app.aupdate_video_index = AsyncMock(
        return_value="job-new",
        side_effect=RuntimeError("submission failed") if fail_submission else None,
    )
    if fail_submission:
        with pytest.raises(RuntimeError, match="submission failed"):
            await app.aupdate_video_global_summary("ws", "asset", "new")
        assert not canonical.exists()
    else:
        assert await app.aupdate_video_global_summary("ws", "asset", "new") == "job-new"
        assert json.loads(canonical.read_text())["metadata"]["global_summary"] == "new"
        assert app.aupdate_video_index.await_args.kwargs["edited_sop_full_path"] == str(canonical)
    assert legacy.read_bytes() == original
