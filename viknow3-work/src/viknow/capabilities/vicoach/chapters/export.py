"""One file-level public list; detailed provenance stays in the learning job."""

import math
from typing import Literal

from pydantic import Field, StrictFloat, StrictInt, model_validator

from .domain import Chapter, Model


class ChapterLocation(Model):
    start: StrictInt | StrictFloat
    end: StrictInt | StrictFloat

    @model_validator(mode="after")
    def ordered(self):
        if not all(math.isfinite(x) for x in (self.start, self.end)):
            raise ValueError("location must be finite")
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid location range")
        return self


class ChapterMetadata(Model):
    file_id: str = Field(min_length=1)
    file_title: str = Field(min_length=1)
    order: StrictInt = Field(ge=1)
    modality: Literal["text", "video"]
    location: ChapterLocation
    source_event_ids: list[StrictInt] = Field(min_length=1)

    @model_validator(mode="after")
    def units_from_modality(self):
        if self.modality == "text":
            if any(type(x) is not int or x < 1 for x in (self.location.start, self.location.end)):
                raise ValueError("text locations must be one-based integer pages")
        elif self.location.end <= self.location.start:
            raise ValueError("video locations must have positive duration in seconds")
        return self


class LearningChapter(Model):
    chapter_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    knowledge_points: list[str]
    metadata: ChapterMetadata


def export_chapters(
    chapters: list[Chapter], file_id: str, file_title: str
) -> list[LearningChapter]:
    if not chapters:
        raise ValueError("cannot publish an empty video chapter list")
    exported, previous = [], None
    for order, chapter in enumerate(chapters, 1):
        if chapter.source_event_ids != list(range(chapter.first_event, chapter.last_event + 1)):
            raise ValueError("export requires ordered contiguous source IDs")
        if previous is not None and abs(chapter.start_seconds - previous) > 0.01:
            raise ValueError("export requires contiguous chapter time ranges")
        previous = chapter.end_seconds
        knowledge = []
        for point in chapter.knowledge_points:
            pieces = [f"{point.name}：{point.explanation}"]
            if point.conditions:
                pieces.append("适用条件：" + "；".join(point.conditions))
            if point.formula:
                pieces.append("公式：" + point.formula)
            if point.examples:
                pieces.append("案例：" + "；".join(point.examples))
            knowledge.append("\n".join(pieces))
        exported.append(
            LearningChapter(
                chapter_id=chapter.chapter_id,
                title=chapter.title,
                description="\n\n".join(
                    f"{s.title}\n{s.body}" for s in chapter.description_sections
                ),
                knowledge_points=knowledge,
                metadata=ChapterMetadata(
                    file_id=file_id,
                    file_title=file_title,
                    order=order,
                    modality="video",
                    location=ChapterLocation(start=chapter.start_seconds, end=chapter.end_seconds),
                    source_event_ids=chapter.source_event_ids,
                ),
            )
        )
    return exported
