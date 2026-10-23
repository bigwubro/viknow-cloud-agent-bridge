"""Opt-in frame evidence enrichment; never edits indexed semantic data."""

import base64
import subprocess
from pathlib import Path

from .client import save


def review_visual_events(events, video_path: Path, client):
    if not video_path.is_file():
        raise FileNotFoundError("visual review requires the original local video")
    selected = [e for e in events if e.uncertainty][: client.config.visual_review_max_events]
    additions, reviews = {}, []
    for event in selected:
        times = [
            event.start_seconds,
            (event.start_seconds + event.end_seconds) / 2,
            max(event.start_seconds, event.end_seconds - 0.1),
        ]
        images, frames = [], []
        for i, timestamp in enumerate(times):
            path = client.run_dir / "visual_frames" / f"event{event.event_id}_{i}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:640:force_original_aspect_ratio=decrease",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            images.append("data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode())
            frames.append({"time_seconds": timestamp, "frame_path": str(path)})
        client.response_schema = {
            "type": "object",
            "properties": {
                "observations": {"type": "string"},
                "unresolved": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["observations", "unresolved"],
            "additionalProperties": False,
        }
        result = client.ask(
            f"visual_event_{event.event_id}",
            "根据按时间顺序的三帧补读屏幕文字、公式或图表。只描述可见证据，不能验证语音。"
            '不代答测验；不确定时明确指出。返回{"observations":"可见内容及对应帧时间","unresolved":[]}。',
            {
                "event_id": event.event_id,
                "frame_times": times,
                "questions": event.uncertainty,
                "_images": images,
            },
            client.config.boundary_output_tokens,
        )
        if not isinstance(result.get("observations"), str) or not result["observations"].strip():
            raise ValueError("visual review returned no observations")
        additions[event.event_id] = result["observations"]
        reviews.append({"event_id": event.event_id, "frames": frames, **result})
        save(client.run_dir / "visual_reviews.json", reviews)
    return additions
