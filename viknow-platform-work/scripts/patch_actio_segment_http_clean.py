"""Patch actio segment.py in hyperrag container for HTTP grant URLs."""

from __future__ import annotations

from pathlib import Path

SEGMENT = Path("/opt/hyperrag/.venv/lib/python3.12/site-packages/actio/tasks/segment.py")

OLD = """    def segment(self, video_path: str | Path, config: SegmentationConfig) -> Dict[str, Any]:
        path = _local_video_path(video_path)
        if not path.exists():
            raise FileNotFoundError(path)
        headers = {"X-API-Key": self.api_key} if self.api_key else None
        payload = {
            "video_path": str(path),
            "segmenter": config.video_split_segmenter,
            "aggregator": config.video_split_aggregator,
            "threshold": config.video_split_threshold,
            "min_duration_s": config.min_duration_s,
            "max_duration_s": float(config.max_duration_s or 30.0),
            "segmentation_mode": config.segmentation_mode,
        }
        upload_mode = (config.video_split_upload_mode or "auto").strip().lower()
        if upload_mode not in {"auto", "path", "base64", "object"}:
            raise ValueError("video_split_upload_mode must be one of: auto, path, base64, object.")
        media_input = self._media_input_for_path(path, config)
        if upload_mode == "object" or (upload_mode == "auto" and media_input):
            if not media_input:
                raise ValueError(
                    "video_split_upload_mode=object requires a matching "
                    "video_split.object_mappings entry."
                )
            payload.pop("video_path")
            payload["media"] = media_input
        elif upload_mode == "base64" or (
            upload_mode == "auto" and config.video_split_upload_base64
        ):
            max_bytes = int(max(0.0, config.video_split_upload_max_mb) * 1024 * 1024)
            size = path.stat().st_size
            if max_bytes and size > max_bytes:
                raise ValueError(
                    f"video file is {size} bytes, exceeds video_split_upload_max_mb="
                    f"{config.video_split_upload_max_mb:g}."
                )
            payload.pop("video_path")
            payload["video_filename"] = path.name
            payload["video_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")"""

NEW = """    def segment(self, video_path: str | Path, config: SegmentationConfig) -> Dict[str, Any]:
        from urllib.parse import urlparse

        raw = str(video_path)
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"}:
            headers = {"X-API-Key": self.api_key} if self.api_key else None
            filename = Path(parsed.path).name or "uploaded.mp4"
            if filename == "content":
                filename = "uploaded.mp4"
            payload = {
                "segmenter": config.video_split_segmenter,
                "aggregator": config.video_split_aggregator,
                "threshold": config.video_split_threshold,
                "min_duration_s": config.min_duration_s,
                "max_duration_s": float(config.max_duration_s or 30.0),
                "segmentation_mode": config.segmentation_mode,
                "media": {"type": "url", "url": raw, "filename": filename},
            }
            path = Path(filename)
        else:
            path = _local_video_path(video_path)
            if not path.exists():
                raise FileNotFoundError(path)
            headers = {"X-API-Key": self.api_key} if self.api_key else None
            payload = {
                "video_path": str(path),
                "segmenter": config.video_split_segmenter,
                "aggregator": config.video_split_aggregator,
                "threshold": config.video_split_threshold,
                "min_duration_s": config.min_duration_s,
                "max_duration_s": float(config.max_duration_s or 30.0),
                "segmentation_mode": config.segmentation_mode,
            }
            upload_mode = (config.video_split_upload_mode or "auto").strip().lower()
            if upload_mode not in {"auto", "path", "base64", "object"}:
                raise ValueError("video_split_upload_mode must be one of: auto, path, base64, object.")
            media_input = self._media_input_for_path(path, config)
            if upload_mode == "object" or (upload_mode == "auto" and media_input):
                if not media_input:
                    raise ValueError(
                        "video_split_upload_mode=object requires a matching "
                        "video_split.object_mappings entry."
                    )
                payload.pop("video_path")
                payload["media"] = media_input
            elif upload_mode == "base64" or (
                upload_mode == "auto" and config.video_split_upload_base64
            ):
                max_bytes = int(max(0.0, config.video_split_upload_max_mb) * 1024 * 1024)
                size = path.stat().st_size
                if max_bytes and size > max_bytes:
                    raise ValueError(
                        f"video file is {size} bytes, exceeds video_split_upload_max_mb="
                        f"{config.video_split_upload_max_mb:g}."
                    )
                payload.pop("video_path")
                payload["video_filename"] = path.name
                payload["video_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")"""


def main() -> None:
    text = SEGMENT.read_text(encoding="utf-8")
    if OLD not in text:
        if 'parsed.scheme in {"http", "https"}' in text:
            print("already patched")
            return
        raise SystemExit("segment block not found")
    SEGMENT.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print("patched segment()")


if __name__ == "__main__":
    main()
