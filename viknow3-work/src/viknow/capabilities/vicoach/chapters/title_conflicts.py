"""Bounded glyph verification of conflicting text within one title occurrence."""

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Annotated

from PIL import Image
from pydantic import Field

from .client import save
from .domain import Model
from .outline import native_frame_path, printed_number, resolve_heading_context


class Characters(Model):
    characters: list[Annotated[str, Field(min_length=1, max_length=1)]]
    readable: bool


def normalized(text):
    return re.sub(r"[\s、，,]+", "", text)


def select_characters(raw, candidates):
    result = Characters.model_validate(raw)
    if not result.readable:
        if result.characters:
            raise ValueError("unreadable title must not guess characters")
        return None
    value = normalized("".join(result.characters))
    for candidate in candidates:
        if normalized(candidate) == value:
            return candidate
    raise ValueError("visible characters do not match any observed candidate")


def conflict_groups(observations, run_dir=None):
    """A missing node ends its occurrence; later revisits never rename earlier ones."""
    active, groups = {}, []
    for index, observation in enumerate(resolve_heading_context(observations)):
        present = {h["node_id"] for h in observation["headings"]}
        active = {key: value for key, value in active.items() if key in present}
        for heading in observation["headings"]:
            key = heading["node_id"]
            if key not in active:
                active[key] = dict(
                    node_id=key,
                    number=heading["number"],
                    indices=[],
                    candidates=[],
                    timestamp=observation["timestamp"],
                )
                groups.append(active[key])
            group = active[key]
            group["indices"].append(index)
            titles = [heading["title"]]
            if run_dir is not None:
                directory = run_dir / "chapter-visual-evidence" / f"{observation['timestamp']:.3f}"
                for path in sorted(directory.glob("region-*.json")):
                    region = json.loads(path.read_text())
                    # Only same-frame source text, never chapter-generated prose.
                    titles.extend(
                        line.strip()
                        for line in region.get("text", "").splitlines()
                        if printed_number(line.strip()) == heading["number"]
                    )
            for title in titles:
                if normalized(title) not in {normalized(x) for x in group["candidates"]}:
                    group["candidates"].append(title)
    return [group for group in groups if len(group["candidates"]) > 1]


def reconcile_titles(observations, video_path, builder):
    updated = copy.deepcopy(observations)
    ledger_path = builder.client.run_dir / "title-text-conflicts.json"
    previous = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    ledger = list(previous)
    for group in conflict_groups(observations, builder.client.run_dir):
        # One bounded request per occurrence/candidate set, even across the two passes.
        identity = {key: group[key] for key in ("node_id", "timestamp")}
        identity["candidates"] = sorted(group["candidates"], key=normalized)
        existing = next((row for row in ledger if row["identity"] == identity), None)
        if existing is None:
            timestamp = group["timestamp"]
            native = native_frame_path(video_path, timestamp, builder.client)
            directory = builder.client.run_dir / "title-text-checks" / f"{timestamp:.3f}"
            directory.mkdir(parents=True, exist_ok=True)
            regions = []
            with Image.open(native) as image:
                width, height = image.size
                boxes = [
                    (0, 0, width, max(1, round(height * 0.6))),
                    (0, min(height - 1, round(height * 0.4)), width, height),
                ]
                for i, bbox in enumerate(boxes):
                    path = directory / f"region-{i}.png"
                    image.crop(bbox).save(path)
                    regions.append(
                        dict(
                            bbox=list(bbox),
                            path=str(path),
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        )
                    )
            prompt = (
                "逐字核对同一教学标题的冲突识读。第一张是完整原图，后两张为原分辨率区域。"
                "候选的顺序和出现次数都不代表正确。只按实际印出的字形逐字符返回characters数组，"
                "包含编号、标点和标题全文；不要按专业常识纠错，不返回候选序号。"
                "字形无法确认则readable=false且characters=[]。"
            )
            payload = dict(
                number=group["number"],
                candidates=group["candidates"],
                timestamp=timestamp,
                source_frame_sha256=hashlib.sha256(native.read_bytes()).hexdigest(),
                regions=[{k: r[k] for k in ("bbox", "sha256")} for r in regions],
                _images=[
                    "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
                    for p in [native, *(Path(r["path"]) for r in regions)]
                ],
            )
            try:
                selected = builder.validated(
                    f"title_text_{timestamp:.3f}",
                    prompt,
                    payload,
                    lambda raw, candidates=group["candidates"]: select_characters(raw, candidates),
                    output=builder.config.boundary_output_tokens,
                    schema=Characters.model_json_schema(),
                )
                error = None
            except (ValueError, OSError) as exc:
                selected, error = None, str(exc)
            existing = dict(
                identity=identity,
                raw_candidates=group["candidates"],
                selected=selected,
                status="verified" if selected else "needs_review",
                frame_path=str(native),
                regions=regions,
                error=error,
            )
            ledger.append(existing)
        for index in group["indices"]:
            item = updated[index]
            item.setdefault("original_heading_titles", {})
            for heading in item["headings"]:
                if heading["number"] == group["number"]:
                    item["original_heading_titles"].setdefault(heading["number"], heading["title"])
                    if existing["selected"]:
                        heading["title"] = existing["selected"]
        if not existing["selected"]:
            issue = f"标题文字待核对（编号 {group['number']}）：{' / '.join(group['candidates'])}"
            for index in group["indices"]:
                issues = updated[index].setdefault("title_review_issues", [])
                if issue not in issues:
                    issues.append(issue)
    save(ledger_path, ledger)
    return updated


def apply_title_results(chapters, observations, duration):
    from .outline import make_spans

    spans = make_spans(observations, duration)
    for chapter in chapters:
        span = next(
            (s for s in spans if s.start_seconds <= chapter.start_seconds < s.end_seconds), None
        )
        if span and span.node_ids:
            chapter.title = span.title
        for observation in observations:
            if chapter.start_seconds <= observation["timestamp"] < chapter.end_seconds:
                for issue in observation.get("title_review_issues", []):
                    if issue not in chapter.review_issues:
                        chapter.review_issues.append(issue)
