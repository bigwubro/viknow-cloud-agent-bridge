"""HyperRAG downloads a gateway object before indexing it."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from viknow3.access.knowledge.service import KnowledgeError, create_index_job
from viknow3.service.hyperrag.client import HyperragClient
from viknow3.service.hyperrag.object_gateway import (
    ObjectFetchError,
    fetch_index_object,
    gateway_from_ops,
    install_object_store,
    reset_object_store,
)


class _MemoryStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def exists(self, key: str) -> bool:
        return key in self.objects

    def get_to_path(self, key: str, path: Path) -> None:
        self.reads.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[key])


class ObjectGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("VIKNOW_SCRATCH_DIR")
        os.environ["VIKNOW_SCRATCH_DIR"] = self._scratch.name
        reset_object_store()

    def tearDown(self) -> None:
        reset_object_store()
        if self._previous is None:
            os.environ.pop("VIKNOW_SCRATCH_DIR", None)
        else:
            os.environ["VIKNOW_SCRATCH_DIR"] = self._previous
        self._scratch.cleanup()

    def test_fetch_writes_the_object_onto_scratch(self) -> None:
        key = "knowledge/assets/asset-1/upload/asset-1.mp4"
        store = _MemoryStore({key: b"video-bytes"})
        install_object_store(store)
        fetched = fetch_index_object(key)
        self.assertEqual(fetched.key, key)
        self.assertEqual(fetched.path.read_bytes(), b"video-bytes")
        self.assertTrue(str(fetched.path).endswith("knowledge/assets/asset-1/upload/asset-1.mp4"))
        again = fetch_index_object(key)
        self.assertEqual(again.path, fetched.path)
        self.assertEqual(store.reads, [key])

    def test_missing_key_is_rejected(self) -> None:
        install_object_store(_MemoryStore({}))
        with self.assertRaises(ObjectFetchError) as caught:
            fetch_index_object("knowledge/assets/missing/upload/missing.mp4")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.body["detail"], "asset_path is not an available storage object key or absolute file path")

    def test_non_knowledge_paths_are_rejected(self) -> None:
        install_object_store(_MemoryStore({"sessions/thread/file.bin": b"x", "/tmp/local.mp4": b"x"}))
        for asset_path in (
            "sessions/thread/file.bin",
            "/tmp/local.mp4",
            "knowledge/../secret.mp4",
            "missing/knowledge/a.mp4",
            "knowledge/assets/a.mp4\x00",
            "s3://bucket/knowledge/assets/a.mp4",
        ):
            with self.subTest(asset_path=asset_path):
                with self.assertRaises(ObjectFetchError) as caught:
                    fetch_index_object(asset_path)
                self.assertEqual(caught.exception.status, 400)

    def test_incomplete_s3_client_is_unavailable(self) -> None:
        with patch(
            "viknow3.service.hyperrag.object_gateway.plain_singleton",
            return_value={"endpoint": "", "bucket": "", "region": "us-east-1", "access_key": None, "secret_key": None},
        ):
            with self.assertRaises(ObjectFetchError) as caught:
                gateway_from_ops()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.body["detail"], "s3_client is incomplete")

    def test_gateway_client_uses_the_internal_endpoint(self) -> None:
        row = {
            "endpoint": "http://127.0.0.1:19191",
            "public_endpoint": "http://36.103.198.236:19191",
            "bucket": "viknow",
            "region": "us-east-1",
            "url_expires_seconds": 3600,
            "access_key": "reader",
            "secret_key": "secret",
        }
        with patch("viknow3.service.hyperrag.object_gateway.plain_singleton", return_value=row):
            with patch("minio.Minio") as minio:
                store = gateway_from_ops()
        args, kwargs = minio.call_args
        self.assertEqual(args[0], "127.0.0.1:19191")
        self.assertFalse(kwargs["secure"])
        self.assertEqual(kwargs["region"], "us-east-1")
        self.assertEqual(kwargs["access_key"], "reader")
        self.assertEqual(kwargs["secret_key"], "secret")
        self.assertEqual(store.bucket, "viknow")

    def test_client_records_the_object_key_on_the_job(self) -> None:
        captured: dict[str, object] = {}

        class _Accepted:
            job_id = "job-1"
            library_id = "library-1"
            asset_id = "asset-1"

        class _Adapter:
            def submit_index(self, **kwargs):
                captured.update(kwargs)
                return _Accepted()

        client = HyperragClient.__new__(HyperragClient)
        client._adapter = _Adapter()
        job = client.create_index(
            "library-1",
            "asset-1",
            "/tmp/viknow-scratch/knowledge/assets/asset-1/upload/asset-1.mp4",
            "动作演示.mp4",
            compile_mode="fast_ingest",
            object_key="knowledge/assets/asset-1/upload/asset-1.mp4",
        )
        self.assertEqual(job["status"], "pending")
        self.assertEqual(captured["asset_path"], "/tmp/viknow-scratch/knowledge/assets/asset-1/upload/asset-1.mp4")
        self.assertEqual(
            captured["params"],
            {"title": "动作演示.mp4", "object_key": "knowledge/assets/asset-1/upload/asset-1.mp4"},
        )
