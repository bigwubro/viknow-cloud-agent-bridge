"""Chapter contracts; model output is validated before publication."""

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChapterConfig(Model):
    ref: str | None = Field(default=None, min_length=1)
    base_url: str | None = None
    base_url_env: str | None = None
    model: str | None = None
    model_env: str | None = None
    token_budget_mode: Literal["vllm", "conservative"] = "vllm"
    image_tokens: int = Field(default=8192, gt=0)
    tokenizer_url: str | None = None
    api_key_env: str | None = None
    context_tokens: int = Field(gt=0)
    input_tokens: int = Field(default=8192, ge=1024)
    output_tokens: int = Field(default=8192, ge=256)
    boundary_output_tokens: int = Field(default=2048, ge=256)
    safety_tokens: int = Field(default=512, ge=0)
    overlap_events: int = Field(default=3, ge=1)
    timeout_seconds: float = Field(default=300, gt=0)
    retries: int = Field(default=1, ge=0, le=3)
    source_manifest: Path | None = None
    visual_review: bool = False
    visual_review_max_events: int = Field(default=3, ge=1)

    def materialize(self):
        """Freeze endpoint/model before durable submission; retain only the key env name."""
        if self.ref:
            raise ValueError("chapter model ref must be resolved by AppConfig")
        values = {}
        for name in ("model", "base_url"):
            explicit = getattr(self, name)
            env_name = getattr(self, name + "_env")
            value = (
                explicit.strip()
                if explicit and explicit.strip()
                else (os.getenv(env_name, "").strip() if env_name else "")
            )
            if not value:
                raise ValueError(
                    f"chapter {name} or its configured environment variable is required"
                )
            values[name] = value
            values[name + "_env"] = None
        return type(self).model_validate({**self.model_dump(), **values})

    @model_validator(mode="after")
    def capacity(self):
        if self.ref and any(
            getattr(self, name) is not None
            for name in (
                "base_url",
                "base_url_env",
                "model",
                "model_env",
                "api_key_env",
                "tokenizer_url",
            )
        ):
            raise ValueError(
                "chapter ref cannot be combined with explicit model connection settings"
            )
        if not self.ref and (
            not (self.model or self.model_env) or not (self.base_url or self.base_url_env)
        ):
            raise ValueError("chapter model and base_url (or their environment names) are required")
        if (
            self.context_tokens
            <= max(self.output_tokens, self.boundary_output_tokens) + self.safety_tokens + 1024
        ):
            raise ValueError("context window cannot fit configured output reserve and input")
        return self


class Event(Model):
    event_id: StrictInt
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    video_id: str
    segment_id: str
    description: str
    asr: str = ""
    ocr_text: str = ""
    ocr_keywords: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    technical_segment_id: str | None = None
    slide_id: str | None = None
    slide_start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    slide_end: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("invalid event time range")
        if (self.slide_start is None) != (self.slide_end is None):
            raise ValueError("slide time range must be complete")
        if self.slide_start is not None and self.slide_end <= self.slide_start:
            raise ValueError("invalid slide time range")
        return self


class Group(Model):
    first_event: StrictInt
    last_event: StrictInt
    title: str = Field(min_length=1)
    rationale: str = ""

    @model_validator(mode="after")
    def range(self):
        if self.first_event < 1 or self.last_event < self.first_event:
            raise ValueError("invalid chapter bounds")
        return self


class Evidence(Model):
    event_id: StrictInt
    field: Literal["asr", "description", "ocr_text", "ocr_keywords", "visual_review"]
    quote: str = Field(min_length=1)
    part_id: str = Field(min_length=1)


class Section(Model):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class KnowledgePoint(Model):
    name: str = Field(min_length=1)
    type: Literal[
        "definition", "principle", "formula", "procedure", "condition", "comparison", "example"
    ]
    explanation: str = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    formula: str | None = None
    examples: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)


class Content(Model):
    knowledge_points: list[KnowledgePoint]
    description_sections: list[Section] = Field(min_length=1)
    title: str = Field(min_length=1)
    review_issues: list[str] = Field(default_factory=list)


class Chapter(Content):
    chapter_id: str
    first_event: StrictInt
    last_event: StrictInt
    start_seconds: float
    end_seconds: float
    source_event_ids: list[StrictInt]
    source_references: list[dict]


def extract_events(payload: dict) -> list[Event]:
    events = []
    for segment in payload.get("segments", []):
        data = segment.get("data") or {}
        visual = data.get("visual_text") or {}
        asr = data.get("asr") or {}
        events.append(
            Event(
                event_id=1,
                start_seconds=segment["time_range"][0],
                end_seconds=segment["time_range"][1],
                video_id=segment.get("video_id") or "",
                segment_id=segment["segment_id"],
                description=str(data.get("caption") or ""),
                asr=asr.get("text", "") if isinstance(asr, dict) else str(asr),
                ocr_text=visual.get("ocr_text", ""),
                ocr_keywords=visual.get("ocr_keywords", []),
                uncertainty=data.get("uncertainty", []),
                technical_segment_id=data.get("technical_segment_id"),
                slide_id=data.get("slide_id"),
                slide_start=data.get("slide_start"),
                slide_end=data.get("slide_end"),
            )
        )
    events.sort(key=lambda e: (e.start_seconds, e.end_seconds))
    if not events or len({e.video_id for e in events}) != 1:
        raise ValueError("expected a nonempty single-video event sequence")
    for i, event in enumerate(events):
        event.event_id = i + 1
        if i and event.start_seconds < events[i - 1].end_seconds - 0.01:
            raise ValueError("overlapping source events")
    return events


def validate_groups(groups: list[Group], first: int, last: int) -> None:
    cursor = first
    for group in groups:
        if group.first_event != cursor or group.last_event > last:
            raise ValueError("chapter event assignment has gaps, overlap or out-of-range IDs")
        cursor = group.last_event + 1
    if not groups or cursor != last + 1:
        raise ValueError("incomplete chapter event coverage")


def validate_expansions(text: str, source: str) -> None:
    """Guard explicit Chinese parenthesized definitions, not general semantic claims."""
    acronym = r"[A-Z]{2,15}"
    chinese = r"[\u4e00-\u9fff]{2,40}"
    forward = re.compile(rf"({chinese})[（(]({acronym})[）)]")
    reverse = re.compile(rf"\b({acronym})[（(]({chinese})[）)]")

    def pairs(value):
        return [(a, name) for name, a in forward.findall(value)] + reverse.findall(value)

    def definition_name(value):
        # Remove sentence-introducing verbs only; do not accept arbitrary suffixes
        # (e.g. adding a modifier to an existing technical full name).
        prefix = re.compile(r"^(?:采用|使用|称为|称作|为了减少|为了降低|减少|降低|提高|这是)")
        while match := prefix.match(value):
            value = value[match.end() :]
        return value

    definitions = [(a, definition_name(name)) for a, name in pairs(source)]
    for abbreviation, name in pairs(text):
        # Only a source abbreviation being expanded is in this narrow contract.
        if not re.search(rf"(?<![A-Za-z0-9]){abbreviation}(?![A-Za-z0-9])", source):
            continue
        if not any(
            a == abbreviation and definition_name(name) == original for a, original in definitions
        ):
            raise ValueError(f"缩写 {abbreviation} 的括号全称未见对应原文；保留原缩写，不补写全称")


def fallback_unsupported_expansions(content: Content, records: list[dict]) -> Content:
    """Replace only an unsupported expansion item with its unchanged source quotes."""
    sources = {(r["event_id"], r["part_id"]): r for r in records}
    for index, item in enumerate([*content.description_sections, *content.knowledge_points], 1):
        source = "\n".join(
            str(sources.get((ref.event_id, ref.part_id), {}).get(ref.field, ""))
            for ref in item.evidence
        )
        text = "\n".join(
            str(value) for key, value in item.model_dump().items() if key != "evidence"
        )
        try:
            validate_expansions(text, source)
        except ValueError as exc:
            # These are program-restored references. validate_content still checks
            # every part/field/verbatim quote afterwards; no invalid evidence is fixed here.
            literal = "来源原文：\n" + "\n".join(dict.fromkeys(ref.quote for ref in item.evidence))
            label = "title" if isinstance(item, Section) else "name"
            try:
                validate_expansions(getattr(item, label), source)
            except ValueError:
                setattr(item, label, "来源原文")
            if isinstance(item, Section):
                item.body = literal
            else:
                item.explanation = literal
                item.conditions, item.examples, item.formula = [], [], None
            issue = f"第{index}项已回退来源原文；{exc}。此回退不表示语义已全部核验。"
            if issue not in content.review_issues:
                content.review_issues.append(issue)
    return content


def validate_content(content: Content, records: list[dict]) -> None:
    sources = {(r["event_id"], r["part_id"]): r for r in records}
    for item in [*content.description_sections, *content.knowledge_points]:
        for ref in item.evidence:
            record = sources.get((ref.event_id, ref.part_id))
            if record is None:
                raise ValueError("evidence outside this chapter or unknown part")
            raw = record.get(ref.field, "")
            if isinstance(raw, list):
                raw = "\n".join(raw)
            if ref.quote not in raw:
                raise ValueError(
                    f"evidence quote not present verbatim: event {ref.event_id} / {ref.field}; "
                    f"copy exact source substring (keep punctuation/newlines): {raw[:160]!r}"
                )
        text = "\n".join(
            str(value) for key, value in item.model_dump().items() if key != "evidence"
        )
        source = "\n".join(
            str(sources[(ref.event_id, ref.part_id)].get(ref.field, "")) for ref in item.evidence
        )
        validate_expansions(text, source)
    # Quote membership establishes provenance, not semantic entailment.


class GroupResponse(Model):
    chapters: list[Group]


class Boundary(Model):
    event_id: StrictInt
    title: str = Field(min_length=1)


class BoundaryResponse(Model):
    boundaries: list[Boundary]


class JointContent(Content):
    first_event: StrictInt
    last_event: StrictInt


class JointResponse(Model):
    chapters: list[JointContent]
