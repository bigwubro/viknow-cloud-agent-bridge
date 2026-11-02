#!/usr/bin/env python3
"""Patch actio to send storage grant URLs to 8600 and download locally for ASR/caption."""

from __future__ import annotations

import sys
from pathlib import Path

SEGMENT_MARKER = "def segment(self, video_path: str | Path, config: SegmentationConfig)"
SEGMENT_INSERT = '''        from urllib.parse import urlparse

        raw = str(video_path)
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"}:
            execution_timeout = config.video_split_service_execution_timeout_s
            execution_timeout = self.timeout_s if execution_timeout is None else execution_timeout
            http_timeout = config.video_split_service_http_timeout_s
            queue_timeout = config.video_split_service_queue_timeout_s
            admission_timeout = config.video_split_service_admission_timeout_s
            headers = {"X-API-Key": self.api_key} if self.api_key else None
            filename = Path(parsed.path).name or "uploaded.mp4"
            payload = {
                "segmenter": config.video_split_segmenter,
                "aggregator": config.video_split_aggregator,
                "threshold": config.video_split_threshold,
                "min_duration_s": config.min_duration_s,
                "max_duration_s": float(config.max_duration_s or 30.0),
                "segmentation_mode": config.segmentation_mode,
                "queue_timeout_s": queue_timeout,
                "execution_timeout_s": execution_timeout,
                "media": {"type": "url", "url": raw, "filename": filename},
            }
            return self._submit_and_wait(payload, headers=headers)

'''


def patch_segment(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "media\": {\"type\": \"url\"" in text and "urlparse(raw)" in text:
        return False
    idx = text.find(SEGMENT_MARKER)
    if idx < 0:
        raise SystemExit(f"segment marker not found in {path}")
    line_end = text.find("\n", idx)
    insert_at = line_end + 1
    path.write_text(text[:insert_at] + SEGMENT_INSERT + text[insert_at:], encoding="utf-8")
    return True


VIDEO_MARKER = "        video_path = Path(video_path)\n        recaption_from_existing"
VIDEO_REPLACEMENT = '''        raw_video_input = str(video_path)
        from urllib.parse import urlparse

        parsed_video = urlparse(raw_video_input)
        segment_video_input: str | Path = video_path
        if parsed_video.scheme in {"http", "https"}:
            intermediate_dir = self._get_intermediate_dir()
            local_name = Path(parsed_video.path).name or "source.mp4"
            local_video = intermediate_dir / local_name
            self._download_http_video(raw_video_input, local_video)
            video_path = local_video
            segment_video_input = raw_video_input
        else:
            video_path = Path(video_path)
        recaption_from_existing'''


HELPER = '''

    @staticmethod
    def _download_http_video(url: str, destination: Path) -> None:
        import urllib.request

        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "actio-video-parser/1.0"})
        with urllib.request.urlopen(request, timeout=600) as response:
            data = response.read()
        if not data:
            raise ValueError("downloaded video is empty")
        destination.write_bytes(data)

'''


def patch_video_parser(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    changed = False
    if "_download_http_video" not in text:
        anchor = "    def _get_intermediate_dir(self)"
        idx = text.find(anchor)
        if idx < 0:
            raise SystemExit(f"video_parser anchor not found in {path}")
        text = text[:idx] + HELPER + text[idx:]
        changed = True
    if VIDEO_MARKER in text and "segment_video_input" not in text:
        text = text.replace(VIDEO_MARKER, VIDEO_REPLACEMENT, 1)
        text = text.replace(
            "                    segment_output = self._segment_video(\n                        video_path,",
            "                    segment_output = self._segment_video(\n                        segment_video_input,",
            1,
        )
        changed = True
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    seg = root / "src/actio/tasks/segment.py"
    vid = root / "src/actio/video_parser.py"
    ok = patch_segment(seg) | patch_video_parser(vid)
    print("patched" if ok else "already patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
