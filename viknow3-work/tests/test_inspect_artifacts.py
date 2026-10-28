"""document_inspect page artifacts in viknow-storage."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from viknow3.agents.runtimes.deepagents.core.knowledge.object_key import (
    parse_storage_reference,
    parse_storage_uri,
)
from viknow3.service.storage.inspect_artifacts import (
    artifact_storage_uri,
    inspect_page_artifact_id,
    upload_inspect_page_artifact,
)


class InspectArtifactIdTests(unittest.TestCase):
    def test_page_artifact_id(self) -> None:
        self.assertEqual(inspect_page_artifact_id(3), "inspect.page-0003.png")


class StorageUriArtifactTests(unittest.TestCase):
    def test_parse_artifact_query(self) -> None:
        uri = artifact_storage_uri("lib-1", "asset-9", "inspect.page-0002.png")
        self.assertEqual(parse_storage_uri(uri), ("lib-1", "asset-9"))
        ref = parse_storage_reference(uri)
        assert ref is not None
        self.assertEqual(ref.artifact_id, "inspect.page-0002.png")


class UploadInspectPageTests(unittest.TestCase):
    @patch("viknow3.service.storage.inspect_artifacts.storage_configured", return_value=True)
    @patch("viknow3.service.storage.inspect_artifacts.get_storage_client")
    def test_upload_replace_on_conflict(self, get_client: MagicMock, _configured: MagicMock) -> None:
        client = MagicMock()
        client.upload_artifact.side_effect = [
            __import__(
                "viknow3.service.storage.client", fromlist=["StorageClientError"]
            ).StorageClientError(409, "exists"),
            {"artifact_id": "inspect.page-0001.png"},
        ]
        get_client.return_value = client
        uri = upload_inspect_page_artifact("lib", "a1", 1, b"\x89PNG\r\n")
        self.assertIn("artifact=inspect.page-0001.png", uri)
        client.delete_artifact.assert_called_once_with("lib", "a1", "inspect.page-0001.png")
        self.assertEqual(client.upload_artifact.call_count, 2)
