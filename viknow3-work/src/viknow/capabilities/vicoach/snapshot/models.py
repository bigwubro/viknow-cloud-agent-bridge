"""Contracts for a versioned course snapshot and original timed ASR."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeRange(Model):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("empty or reversed time range")
        return self


class SourceReference(Model):
    segment_id: str
    time_range: tuple[float, float]


class Event(TimeRange):
    event_id: int = Field(gt=0)
    segment_id: str
    video_id: str
    description: str
    asr: str
    ocr_keywords: list[str]
    ocr_text: str
    uncertainty: list[str]
    technical_segment_id: str | None = None
    slide_id: str | None = None
    slide_start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    slide_end: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def slide_range(self):
        if (self.slide_start is None) != (self.slide_end is None):
            raise ValueError("slide time range must be complete")
        if self.slide_start is not None and self.slide_end <= self.slide_start:
            raise ValueError("invalid slide time range")
        return self


class DescriptionSection(Model):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


class SnapshotChapter(TimeRange):
    chapter_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str
    description_sections: list[DescriptionSection] = Field(default_factory=list)
    first_event: int = Field(gt=0)
    last_event: int = Field(gt=0)
    source_event_ids: list[int] = Field(min_length=1)
    source_references: list[SourceReference]


class AsrSegment(TimeRange):
    segment_id: str
    event_id: int | None = None
    source_event_ids: list[int] = Field(default_factory=list)
    timing_precision: str = "asr_segment"
    text: str = Field(min_length=1)


class Lesson(Model):
    schema_version: Literal["course-lesson.v1"] = "course-lesson.v1"
    asset_id: str
    sample: str
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    chapter_version: str
    chapters: list[SnapshotChapter] = Field(min_length=1)
    events: list[Event] = Field(min_length=1)
    asr_segments: list[AsrSegment]
    transcript_source: Literal["sop_full_asr_segments", "video_transcript"] = (
        "sop_full_asr_segments"
    )
    transcript_version: str | None = None
    transcript_timing: Literal["asr_segment"] = "asr_segment"
    transcript_sentences: list[dict] = Field(default_factory=list)
    course_background: dict = Field(default_factory=dict)
    processing: dict | None = None
    outline: dict | None = None

    @model_validator(mode="after")
    def coverage(self):
        ids = [event.event_id for event in self.events]
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError("events must have ordered unique IDs")
        previous = 0
        for event in self.events:
            if (
                event.video_id != self.asset_id
                or event.start_seconds < previous - 0.01
                or event.end_seconds > self.duration_seconds + 0.01
            ):
                raise ValueError("event source or timing mismatch")
            previous = event.end_seconds
        assigned, chapter_ids, previous = [], set(), 0
        for chapter in self.chapters:
            if (
                chapter.chapter_id in chapter_ids
                or abs(chapter.start_seconds - previous) > 0.01
                or chapter.source_event_ids
                != list(range(chapter.first_event, chapter.last_event + 1))
            ):
                raise ValueError("chapter identity or coverage mismatch")
            chapter_ids.add(chapter.chapter_id)
            assigned.extend(chapter.source_event_ids)
            events = [
                e
                for e in self.events
                if e.start_seconds < chapter.end_seconds and e.end_seconds > chapter.start_seconds
            ]
            if [e.event_id for e in events] != chapter.source_event_ids:
                raise ValueError("chapter must reference every overlapping source event")
            refs = [
                SourceReference(
                    segment_id=e.segment_id,
                    time_range=(e.start_seconds, e.end_seconds),
                )
                for e in events
            ]
            if refs != chapter.source_references:
                raise ValueError("chapter provenance mismatch")
            previous = chapter.end_seconds
        if sorted(set(assigned)) != ids or abs(previous - self.duration_seconds) > 0.01:
            raise ValueError("incomplete chapter coverage")
        segment_ids, previous = set(), 0
        event_map = {e.event_id: e for e in self.events}
        for segment in self.asr_segments:
            if self.transcript_source == "video_transcript":
                related = [
                    e.event_id
                    for e in self.events
                    if e.end_seconds > segment.start_seconds
                    and e.start_seconds < segment.end_seconds
                ]
                if (
                    segment.segment_id in segment_ids
                    or not segment.text.strip()
                    or segment.start_seconds < previous
                    or segment.end_seconds > self.duration_seconds + 0.01
                    or segment.source_event_ids != related
                ):
                    raise ValueError("global ASR provenance or timing mismatch")
                segment_ids.add(segment.segment_id)
                previous = segment.start_seconds
                continue
            event = event_map.get(segment.event_id)
            if (
                event is None
                or segment.segment_id in segment_ids
                or not segment.text.strip()
                or segment.start_seconds < previous - 0.01
                or segment.start_seconds < event.start_seconds - 0.01
                or segment.end_seconds > event.end_seconds + 0.01
            ):
                raise ValueError("ASR provenance or timing mismatch")
            segment_ids.add(segment.segment_id)
            previous = segment.end_seconds
        return self
