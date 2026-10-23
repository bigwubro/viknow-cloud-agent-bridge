"""Assemble a frozen course from verified source references, without running ASR.

The caller supplies the bundle directory. No deployment paths, web framework,
knowledge runtime or generation jobs are needed to read this snapshot.
"""

import hashlib
import json
from pathlib import Path

from .models import AsrSegment, Lesson


def build_lesson(
    *, manifest: dict, sop: dict, events: list[dict], chapters: list[dict], chapter_version: str
) -> Lesson:
    source_segments = {segment["segment_id"]: segment for segment in sop["segments"]}
    if len(source_segments) != len(sop["segments"]):
        raise ValueError("duplicate video segment")
    segments = []
    for event in events:
        source_segment = source_segments[event["segment_id"]]
        if source_segment["video_id"] != manifest["asset_id"] or source_segment["time_range"] != [
            event["start_seconds"],
            event["end_seconds"],
        ]:
            raise ValueError("event differs from source segment")
        if "transcript" in sop:
            continue
        for index, segment in enumerate(source_segment["data"]["asr"]["segments"], 1):
            segments.append(
                AsrSegment(
                    segment_id=f"e{event['event_id']}s{index}",
                    event_id=event["event_id"],
                    start_seconds=segment["start_time"],
                    end_seconds=segment["end_time"],
                    text=segment["text"],
                )
            )
    canonical = sop.get("transcript")
    extra = {}
    if canonical is not None:
        if (
            canonical.get("schema_version") != "video-transcript.v1"
            or canonical.get("video_id") != manifest["asset_id"]
        ):
            raise ValueError("canonical transcript belongs to another video")
        expected_version = hashlib.sha256(
            json.dumps(canonical["segments"], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if canonical.get("version") != expected_version:
            raise ValueError("canonical transcript version checksum mismatch")
        segments = [
            AsrSegment(
                segment_id=item["segment_id"],
                start_seconds=item["start_time"],
                end_seconds=item["end_time"],
                text=item["text"],
                timing_precision=item.get("timing_precision", "asr_segment"),
                source_event_ids=[
                    e["event_id"]
                    for e in events
                    if e["end_seconds"] > item["start_time"]
                    and e["start_seconds"] < item["end_time"]
                ],
            )
            for item in canonical["segments"]
        ]
        events = [
            {
                **event,
                "asr": " ".join(
                    item.text for item in segments if event["event_id"] in item.source_event_ids
                ),
            }
            for event in events
        ]
        extra = {
            "transcript_source": "video_transcript",
            "transcript_version": canonical["version"],
        }
    if "transcript_version" not in extra:
        from ..chapters.jobs import fingerprint

        extra["transcript_version"] = fingerprint([s.model_dump() for s in segments])
    return Lesson(
        asset_id=manifest["asset_id"],
        sample=manifest["sample"],
        duration_seconds=manifest["duration_seconds"],
        chapter_version=chapter_version,
        chapters=chapters,
        events=events,
        asr_segments=segments,
        **extra,
    )


def load_lesson(bundle: Path) -> Lesson:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    source = (bundle / "sop-full.json").read_bytes()
    chapters = (bundle / "chapters.json").read_bytes()
    events = (bundle / "events.json").read_bytes()
    for name, content in (
        ("sop-full.json", source),
        ("chapters.json", chapters),
        ("events.json", events),
    ):
        if hashlib.sha256(content).hexdigest() != manifest["sha256"][name]:
            raise ValueError("course source checksum mismatch")
    sop = json.loads(source)
    if "transcript.json" in manifest["sha256"]:
        transcript = (bundle / "transcript.json").read_bytes()
        if hashlib.sha256(transcript).hexdigest() != manifest["sha256"]["transcript.json"]:
            raise ValueError("course transcript checksum mismatch")
        sop["transcript"] = json.loads(transcript)
    return build_lesson(
        manifest=manifest,
        sop=sop,
        events=json.loads(events),
        chapters=json.loads(chapters),
        chapter_version=hashlib.sha256(chapters).hexdigest(),
    )
