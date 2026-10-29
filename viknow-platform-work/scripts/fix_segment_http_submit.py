from pathlib import Path

p = Path("/opt/hyperrag/.venv/lib/python3.12/site-packages/actio/tasks/segment.py")
t = p.read_text(encoding="utf-8")
old = """            payload = {
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
        upload_mode = (config.video_split_upload_mode or "auto").strip().lower()"""
new = """            payload = {
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
            path = Path(filename)
        else:
            path = _local_video_path(video_path)
            if not path.exists():
                raise FileNotFoundError(path)
            payload = {
                "video_path": str(path),
                "segmenter": config.video_split_segmenter,
                "aggregator": config.video_split_aggregator,
                "threshold": config.video_split_threshold,
                "min_duration_s": config.min_duration_s,
                "max_duration_s": float(config.max_duration_s or 30.0),
                "segmentation_mode": config.segmentation_mode,
            }
        headers = {"X-API-Key": self.api_key} if self.api_key else None
        if parsed.scheme not in {"http", "https"}:
            upload_mode = (config.video_split_upload_mode or "auto").strip().lower()"""
if old not in t:
    if "if parsed.scheme not in {\"http\", \"https\"}:" in t:
        print("already merged")
    else:
        raise SystemExit("merge marker missing")
else:
    p.write_text(t.replace(old, new, 1), encoding="utf-8")
    print("merged http submit path")
