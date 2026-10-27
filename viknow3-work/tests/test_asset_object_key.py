"""Inspect and citation payloads carry catalog object keys, not /media URLs."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.runtimes.deepagents.core.answer_review.claim_bundle import citation_payload
from agents.runtimes.deepagents.core.evidence import Evidence, EvidencePart, EvidencePartKind, EvidenceTimeRange
from agents.runtimes.deepagents.core.knowledge.object_key import knowledge_object_key, resolve_asset_object_key
from agents.runtimes.deepagents.core.tools.knowledge import _segment_preview_payload
from agents.runtimes.deepagents.core.tools.multimodal import (
    _enrich_evidence_with_video_url,
    _inspect_claim_render,
    _inspect_claim_source,
    _library_asset_document_url,
    _library_asset_video_url,
)
from agents.runtimes.deepagents.core.tools.segment_catalog import SegmentRecord

KEY = "knowledge/assets/asset-1/upload/asset-1.normalized.mp4"
PDF = "knowledge/assets/asset-1/upload/asset-1.pdf"


def _video_evidence(*, asset_path: str | None = KEY, video_url: str | None = None) -> Evidence:
    metadata = {
        "library_id": "video-lib",
        "asset_id": "asset-1",
        "begin_ts": 2.0,
        "end_ts": 5.0,
    }
    if asset_path:
        metadata["asset_path"] = asset_path
    if video_url:
        metadata["video_url"] = video_url
    return Evidence(
        id="chunk-1",
        source="knowledge_search",
        title="clip.mp4",
        metadata=metadata,
        parts=[
            EvidencePart(
                kind=EvidencePartKind.VIDEO,
                uri="/tmp/clip.mp4",
                time_range=EvidenceTimeRange(start_seconds=2.0, end_seconds=5.0),
                metadata={"asset_id": "asset-1"},
            )
        ],
    )


class ObjectKeyTests(unittest.TestCase):
    def test_rejects_media_and_local_paths(self) -> None:
        self.assertIsNone(knowledge_object_key("/media/libraries/lib/assets/asset-1/video"))
        self.assertIsNone(knowledge_object_key("/tmp/clip.mp4"))
        self.assertEqual(knowledge_object_key(f"/{KEY}#t=1,2"), KEY)

    def test_video_inspect_source_uses_the_object_key(self) -> None:
        evidence = _enrich_evidence_with_video_url(_video_evidence())
        claim = SimpleNamespace(timestamp_start=3.0, timestamp_end=4.0, claim="有人在走")
        source = _inspect_claim_source("video_inspect", claim, evidence)
        render = _inspect_claim_render("video_inspect", claim, evidence, source)
        dumped = json.dumps(source.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("/media/", dumped)
        self.assertEqual(source.asset_path, KEY)
        self.assertEqual(source.timestamp_start, 3.0)
        self.assertEqual(source.anchors[0]["video_url"], KEY)
        self.assertEqual(source.anchors[0]["timestamp_start"], 3.0)
        self.assertEqual(render.url, KEY)
        self.assertEqual(evidence.metadata["video_url"], KEY)

    def test_existing_media_url_is_replaced(self) -> None:
        evidence = _enrich_evidence_with_video_url(
            _video_evidence(video_url="/media/libraries/video-lib/assets/asset-1/video#t=2.0,5.0")
        )
        self.assertEqual(evidence.metadata["video_url"], KEY)
        self.assertNotIn("/media/", json.dumps(evidence.metadata))

    def test_catalog_lookup_supplies_the_key(self) -> None:
        evidence = _video_evidence(asset_path=None)
        with patch(
            "agents.runtimes.deepagents.core.knowledge.object_key.catalog_asset_object_key",
            return_value=KEY,
        ):
            self.assertEqual(_library_asset_video_url(evidence), KEY)

    def test_media_url_without_a_catalog_key_is_removed(self) -> None:
        evidence = _video_evidence(
            asset_path=None,
            video_url="/media/libraries/video-lib/assets/asset-1/video",
        )
        with patch(
            "agents.runtimes.deepagents.core.knowledge.object_key.catalog_asset_object_key",
            return_value=None,
        ):
            enriched = _enrich_evidence_with_video_url(evidence)
        self.assertNotIn("video_url", enriched.metadata)
        self.assertNotIn("/media/", json.dumps(enriched.model_dump(mode="json")))

    def test_missing_key_does_not_invent_a_media_url(self) -> None:
        evidence = _video_evidence(asset_path=None)
        with patch(
            "agents.runtimes.deepagents.core.knowledge.object_key.catalog_asset_object_key",
            return_value=None,
        ):
            self.assertIsNone(_library_asset_video_url(evidence))
            enriched = _enrich_evidence_with_video_url(evidence)
        self.assertNotIn("video_url", enriched.metadata)
        self.assertNotIn("/media/", json.dumps(enriched.model_dump(mode="json")))

    def test_document_preview_is_the_original_object(self) -> None:
        evidence = Evidence(
            id="doc-1",
            source="knowledge_search",
            title="notes.pdf",
            metadata={
                "library_id": "docs",
                "asset_id": "asset-1",
                "asset_path": PDF,
                "document_inspect_page": 3,
                "file_path": "/tmp/notes.pdf",
            },
        )
        self.assertEqual(_library_asset_document_url(evidence), PDF)
        source = _inspect_claim_source("document_inspect", SimpleNamespace(claim="表格"), evidence)
        dumped = json.dumps(source.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("/media/", dumped)
        self.assertEqual(source.anchors[0]["document_url"], PDF)
        self.assertNotIn("page_image_url", source.anchors[0])

    def test_image_citation_uses_the_catalog_key(self) -> None:
        with patch(
            "agents.runtimes.deepagents.core.knowledge.object_key.catalog_asset_object_key",
            return_value="knowledge/assets/asset-1/upload/asset-1.png",
        ):
            payload = citation_payload(
                "[1]",
                {
                    "tool": "image_inspect",
                    "claim": "示意图",
                    "display_label": "图片",
                    "source": {
                        "type": "image",
                        "library_id": "lib",
                        "asset_id": "asset-1",
                        "source_image_id": "figure.png",
                        "uri": "/tmp/figure.png",
                    },
                },
            )
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("/media/", dumped)
        self.assertEqual(
            payload["source"]["anchors"][0]["image_url"],
            "knowledge/assets/asset-1/upload/asset-1.png",
        )

    def test_media_byte_routes_are_not_mounted(self) -> None:
        from viknow3.access.knowledge import routes
        from viknow3.server import create_app

        self.assertFalse(hasattr(routes, "media_router"))
        paths = {getattr(route, "path", "") for route in create_app().routes}
        self.assertFalse(any(str(path).startswith("/media/") for path in paths))

    def test_segment_payload_carries_the_object_key(self) -> None:
        record = SegmentRecord(
            segment_id="seg-1",
            order=0,
            summary="第三秒",
            time_range={"start_seconds": 2.0, "end_seconds": 4.0},
        )
        payload = _segment_preview_payload(record, KEY)
        self.assertEqual(payload["asset_path"], KEY)
        self.assertEqual(payload["time_range"]["start_seconds"], 2.0)
        self.assertIsNone(resolve_asset_object_key("", "", "/media/libraries/a/assets/b/video"))


if __name__ == "__main__":
    unittest.main()
