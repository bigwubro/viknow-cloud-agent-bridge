"""Real isolated Redis checks for the scheduler-core transaction adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from unittest.mock import patch

import pytest
import redis
from scheduler_core.config import SchedulerConfig
from scheduler_core.models import Task as UnifiedTask
from scheduler_core.models import TaskKind, Tier
from scheduler_core.store import SchedulerStore

from hyperrag.task.models import Task, TaskResource, TaskStatus
from hyperrag.task.unified_completion import finish_owned_task


@pytest.fixture
def isolated_store(tmp_path):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for isolated transaction test")
    socket = str(tmp_path / "redis.sock")
    proc = subprocess.Popen(
        [binary, "--port", "0", "--unixsocket", socket, "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    try:
        for _ in range(100):
            try:
                client.ping()
                break
            except redis.ConnectionError:
                time.sleep(0.01)
        else:
            pytest.fail("isolated Redis did not start")
        yield SchedulerStore(client, SchedulerConfig())
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=5)


def setup_task(store, status=TaskStatus.COMPLETED):
    task = UnifiedTask(
        id="t1", kind=TaskKind.JOB_TASK, tier=Tier.BACKGROUND, domains=["vllm"], job_id="j1"
    )
    store.create_task(task)
    store.client.hset(
        "scheduler:task:t1", mapping={"status": "running", "started_at": "100.25", "seq": "7"}
    )
    source = Task(
        task_id="t1",
        task_type="video_parse",
        workspace="w",
        asset_id="a1",
        job_id="j1",
        status=status,
        resource=TaskResource(),
        metadata={"_unified_admission": {"started_at": 100.25, "seq": 7}},
        started_at=100.3,
        completed_at=101,
    )
    store.client.set("hyperrag:task:t1", source.model_dump_json())
    downstream = UnifiedTask(
        id="t2",
        kind=TaskKind.JOB_TASK,
        tier=Tier.BACKGROUND,
        domains=["vllm"],
        job_id="j1",
        depends_on=["t1"],
    )
    store.create_task(downstream)
    store.client.hset("scheduler:dag:j1", mapping={"t1": "", "t2": "t1"})
    return task, source


def finish(store, task, source, **kwargs):
    return finish_owned_task(store, task, started_at=100.25, seq=7, hyperrag_task=source, **kwargs)


def test_real_complete_missing_lease_then_done_is_idempotent(isolated_store):
    store = isolated_store
    task, source = setup_task(store)
    assert finish(store, task, source) == "completed"
    assert store.task_status("t1") == "done"
    assert store.task_status("t2") == "queued"
    store.client.hset("scheduler:task:t2", "status", "running")
    seq = store.client.get("scheduler:seq")
    assert finish(store, task, source) == "already_done"
    assert store.task_status("t2") == "running"
    assert store.client.get("scheduler:seq") == seq


@pytest.mark.parametrize("change", ["started_at", "seq", "lease", "source"])
def test_real_new_admission_or_source_rejected(isolated_store, change):
    store = isolated_store
    task, source = setup_task(store)
    if change == "started_at":
        store.client.hset("scheduler:task:t1", "started_at", "200")
    elif change == "seq":
        store.client.hset("scheduler:task:t1", "seq", "8")
    elif change == "lease":
        store.client.hset("scheduler:lease:meta:t1", "acquired_at", "200")
    else:
        raw = source.model_dump(mode="json")
        raw["asset_id"] = "other"
        store.client.set("hyperrag:task:t1", json.dumps(raw))
    assert finish(store, task, source) in {"ownership_changed", "source_mismatch"}
    assert store.task_status("t1") == "running"
    assert store.task_status("t2") == "pending_deps"


def test_real_takeover_between_watch_and_exec_aborts_lua(isolated_store):
    store = isolated_store
    task, source = setup_task(store)
    script = store.scripts.complete

    def race(**kwargs):
        # A different Redis connection mutates the watched generation after
        # our validation. Actual EVALSHA must stay queued, not run immediately.
        store.client.hset("scheduler:task:t1", "started_at", "200")
        return script(**kwargs)

    with patch.object(store.scripts, "complete", side_effect=race):
        assert finish(store, task, source) == "ownership_changed"
    assert store.task_status("t1") == "running"
    assert store.task_status("t2") == "pending_deps"
    assert store.client.get("scheduler:seq") is None


@pytest.mark.parametrize("status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_real_failed_cancelled_do_not_release_dependencies(isolated_store, status):
    store = isolated_store
    task, source = setup_task(store, status)
    assert finish(store, task, source) == status.value
    assert store.task_status("t1") == status.value
    assert store.task_status("t2") == "pending_deps"


def test_recovery_refuses_active_lease_and_dry_run_does_not_write(isolated_store):
    store = isolated_store
    task, source = setup_task(store)
    assert finish(store, task, source, require_expired=True, dry_run=True) == "eligible"
    assert store.task_status("t1") == "running"
    store.client.hset(
        "scheduler:lease:meta:t1",
        mapping={"acquired_at": "100.25", "domains": "vllm", "kind": "job_task"},
    )
    assert finish(store, task, source, require_expired=True) == "active_lease"
    assert store.task_status("t2") == "pending_deps"


def test_source_admission_mismatch_even_when_terminal(isolated_store):
    store = isolated_store
    task, source = setup_task(store)
    source.metadata["_unified_admission"]["started_at"] = 999
    store.client.set("hyperrag:task:t1", source.model_dump_json())
    assert finish(store, task, source) == "source_admission_mismatch"
    assert store.task_status("t2") == "pending_deps"


def test_legacy_requires_explicit_pinned_source_and_expired_lease(isolated_store):
    store = isolated_store
    task, source = setup_task(store)
    source.metadata = {}
    store.client.set("hyperrag:task:t1", source.model_dump_json())
    assert finish(store, task, source) == "source_admission_mismatch"
    assert (
        finish(store, task, source, require_expired=True, legacy_source_started_at=200)
        == "source_admission_mismatch"
    )
    assert (
        finish(store, task, source, require_expired=True, legacy_source_started_at=100.3)
        == "completed"
    )
    assert store.task_status("t2") == "queued"


def test_original_job_identity_and_stale_domain_member(isolated_store):
    store = isolated_store
    task, source = setup_task(store)
    task.job_id = "another-job"
    assert finish(store, task, source) == "source_mismatch"
    task.job_id = "j1"
    store.client.zadd("scheduler:lease:vllm:job_task", {"t1": 100.25, "other-task": 200})
    assert finish(store, task, source) == "completed"
    assert store.client.zscore("scheduler:lease:vllm:job_task", "t1") is None
    assert store.client.zscore("scheduler:lease:vllm:job_task", "other-task") == 200
