from pathlib import Path

p = Path("/home/cursor-agent/src/viknow3/external/actio/src/actio/tasks/segment.py")
text = p.read_text(encoding="utf-8")
old = (
    "def segment_video(config: SegmentationConfig, video_path: str) -> Dict[str, Any]:\n"
    "    local_path = _local_video_path(video_path)\n"
    "    if config.segment_backend == \"fixed\":"
)
new = (
    "def segment_video(config: SegmentationConfig, video_path: str) -> Dict[str, Any]:\n"
    "    raw = str(video_path)\n"
    "    parsed = urlparse(raw)\n"
    "    if config.segment_backend == \"video_split_remote\" and parsed.scheme in {\"http\", \"https\"}:\n"
    "        if not config.video_split_service_url:\n"
    "            raise ValueError(\n"
    "                \"video_split_service_url is required when segment_backend is video_split_remote.\"\n"
    "            )\n"
    "        logger.info(\"Requesting semantic video splitting (media url): %s\", config.video_split_service_url)\n"
    "        return VideoSplitSegmentClient(\n"
    "            config.video_split_service_url,\n"
    "            config.video_split_service_api_key,\n"
    "            config.video_split_service_timeout_s,\n"
    "            config.video_split_service_poll_interval_s,\n"
    "            config.video_split_service_max_retries,\n"
    "            config.video_split_service_retry_delay_s,\n"
    "            config.video_split_service_trust_env_proxy,\n"
    "        ).segment(raw, config)\n"
    "    local_path = _local_video_path(video_path)\n"
    "    if config.segment_backend == \"fixed\":"
)
if old not in text:
    raise SystemExit("marker missing")
p.write_text(text.replace(old, new, 1), encoding="utf-8")
print("ok")
