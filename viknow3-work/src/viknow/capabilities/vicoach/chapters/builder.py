"""Bounded chapter partitioning and evidence-grounded content generation."""

import copy
import hashlib
import json
import re
import urllib.error
import uuid

from .client import InputTooLong, OutputTooLong, dumps, save
from .domain import (
    BoundaryResponse,
    Chapter,
    Content,
    Group,
    GroupResponse,
    JointResponse,
    extract_events,
    fallback_unsupported_expansions,
    validate_content,
    validate_groups,
)

VERSION = "vicoach-chapters-v12"
GROUP_PROMPT = """按完整语义主题对连续events分章。不要将每个动作或每个幻灯片机械分章。
每个event恰好一次，不跨时间合并；同event的多个part属于同章。首尾可能是跨块的半章。
明确的广告/赞助段与教学内容分别成章。围绕同一概念的定义、解释和例子通常属于一章，
一个比较问题的不同情形通常仍属于同章；只有转向不同的核心主题才另起章。
返回{"chapters":[{"first_event":整数,"last_event":整数,"title":"短标题","rationale":"主题及边界理由"}]}。
使用输入原始event_id；完整覆盖当前输入event范围，不生成时间戳。"""
SEAM_PROMPT = """复核相邻分块接缝。分块边缘不是章节边界，连续主题应合并；不同主题应保留或移动边界。
依据原始events和候选主题，返回当前窗口内部的全部章节起始event_id与短标题。
不要输出窗口第一个event作为边界。不要在同event的不同part之间切章。
广告/赞助与教学分开；同一核心主题的定义、解释、例子保持整体，避免细碎章节。
格式{"boundaries":[{"event_id":整数,"title":"短标题"}]}，没有内部边界则空数组。"""
CONTENT_PROMPT = """根据本章原始events整理完整知识点与章节背景描述。章节范围固定。
课程背景只调整表达重点和难度，不能替代源证据。若chapter_position给出时间范围，
只写该时间内的主题；chapter_overlap标明源事件与本章交集，事件可能跨越多个PPT标题，
不要把相邻编号小节的内容归入本章。
description_sections按讲解顺序组织为有独立标题的学习笔记，每节围绕一个明确主题展开。
正文直接说明本章的方法、概念、步骤或演示内容；人物、事件、环境仅在有助于理解时描述，源中没有的背景不补写。
避免反复使用“该部分展示了”“画面直观地展示了”“在技术实现细节上”等引导套话，不重复标题。
同节内按原文支持的逻辑衔接定义、做法、条件和例子；不同主题分节，正文可用空行分段。
不能为求行文连贯而编造因果、目的、过渡事实，或删掉公式、数字、限制条件及来源冲突。
knowledge_points完整展开实际讲述的知识，不仅列关键词。最后确定标题。保留定义、解释、条件、步骤、
公式、变量、单位、具体数字和例子；只写源内容，不凑字数。不只改写知识清单。
正文、标题和解释全部用中文，专业术语可保留英文。
知识点必须是本章实际解释的完整知识；广告、课程预告、仅提到的下期主题不算知识。
片头、广告、告别知识数组可为空。测验出现须写入正文；只记录源中可见题干，
看不清则说明缺失，未给答案不能代答，不能只将测验藏在review_issues。
根据chapter_position判断全视频位置：中间章节不能因局部描述写“结尾”就声称视频结束。
只标记本章证据本身的缺失/冲突，不把未在本章展开的后续主题当作遗漏。
每条evidence合起来须支持本节/知识点的全部解释、条件、公式和例子；
不同例子的数字来自不同event时分别引用，不能只引用一句概括。除不尽的数值保留近似符号。
每节及每个知识点必须选择本章events.excerpts中的证据；只返回excerpt_id，不要自行生成引用文本、字段或事件编号。
输出{"knowledge_points":[{"name":"名称","type":"definition/principle/formula/procedure/condition/comparison/example之一",
"explanation":"完整解释","conditions":[],"formula":null,"examples":[],
"evidence":[{"excerpt_id":"输入提供的ID"}]}],
"description_sections":[{"title":"小节","body":"充分展开的笔记",
"evidence":[{"excerpt_id":"输入提供的ID"}]}],
"title":"章节标题","review_issues":["具体缺失或冲突，无则空数组"]}。
evidence字段只是来源锚点，不自动证明结论正确。"""
FACT_PROMPT = (
    CONTENT_PROMPT
    + """
本次仅整理知识和原始证据清单：description_sections作为按顺序排列的事实条目，
每条尽量单一事实，保留全部教学细节，不做压缩性摘要。"""
)
WRITE_PROMPT = (
    CONTENT_PROMPT
    + """
已提供knowledge_draft作为覆盖清单，请回读原始events写笔记；不得引入清单里无原始依据的内容。"""
)


def review_content_quality(content, position=None):
    prose = [s.body for s in content.description_sections] + [
        k.explanation for k in content.knowledge_points
    ]
    if any(not re.search(r"[\u4e00-\u9fff]", text) for text in prose):
        raise ValueError("正文和知识解释必须用中文，quote保留源语言，请翻译英文正文")
    # Temporal language can be a negation or a future preview. Flag, never infer error
    # from keyword presence alone.
    if (
        position
        and position["chapter_index"] < position["chapter_count"]
        and any(
            term in text
            for text in prose
            for term in ("视频结尾", "结束视频", "视频的结束", "视频结束")
        )
    ):
        issue = "非末章出现视频结束相关表述，请核验其为预告、否定还是错误结束断言。"
        if issue not in content.review_issues:
            content.review_issues.append(issue)


def record_from_event(event):
    return {
        "event_id": event.event_id,
        "part_id": f"e{event.event_id}",
        "start_seconds": event.start_seconds,
        "end_seconds": event.end_seconds,
        **{k: getattr(event, k) for k in ("description", "asr", "ocr_text", "uncertainty")},
        # Show the exact quotable text; the source Event keeps its OCR list.
        "ocr_keywords": "\n".join(event.ocr_keywords),
    }


def split_record(record):
    """Lossless field split; both children keep parent event/time identity."""
    candidates = {
        k: v
        for k, v in record.items()
        if k in {"description", "asr", "ocr_text", "ocr_keywords", "uncertainty", "visual_review"}
        and v
    }
    if not candidates:
        raise InputTooLong("event metadata alone exceeds budget")
    key = max(candidates, key=lambda k: len(dumps(candidates[k])))
    value = record[key]
    if len(value) < 2:
        # Separate fields rather than discard source evidence.
        if len(candidates) < 2:
            raise InputTooLong("cannot split evidence further")
        left, right = dict(record), dict(record)
        for field in candidates:
            (right if field == key else left)[field] = [] if isinstance(record[field], list) else ""
    else:
        mid = len(value) // 2
        if isinstance(value, str):
            # Prefer a sentence/word boundary near the midpoint; preserve every character.
            points = [value.rfind(c, max(1, mid // 2), mid + 1) for c in ("。", ".", "\n", " ")]
            cut = max(points)
            if cut > 0:
                mid = cut + 1
        left, right = dict(record), dict(record)
        left[key], right[key] = value[:mid], value[mid:]
        # Other fields belong to left only, avoiding amplification while recursing.
        for field in candidates:
            if field != key:
                right[field] = [] if isinstance(record[field], list) else ""
    left["part_id"], right["part_id"] = record["part_id"] + ".a", record["part_id"] + ".b"
    return left, right


EVIDENCE_FIELDS = ("description", "asr", "ocr_text", "ocr_keywords", "visual_review")


def evidence_catalogue(records):
    """Only source-owned excerpts cross the model boundary; final quotes stay literal."""
    wire, catalogue = [], {}
    for record in records:
        item = {
            k: v for k, v in record.items() if k not in EVIDENCE_FIELDS and k != "frame_evidence"
        }
        item["excerpts"] = []
        for field in EVIDENCE_FIELDS:
            # Keyword lists lose layout and condition/contrast relationships.
            # Keep them in indexed records for discovery, never feed them as facts.
            if field == "ocr_keywords":
                continue
            for index, excerpt in enumerate([record.get(field, "")]):
                if not excerpt.strip():
                    continue
                key = f"{record['part_id']}:{field}:{index}"
                catalogue[key] = dict(
                    event_id=record["event_id"],
                    part_id=record["part_id"],
                    field=field,
                    quote=excerpt,
                )
                item["excerpts"].append(dict(excerpt_id=key, field=field, text=excerpt))
        wire.append(item)
    return wire, catalogue


def selection_schema(model, catalogue):
    schema = model.model_json_schema()
    schema["$defs"]["Evidence"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"excerpt_id": {"type": "string", "enum": list(catalogue)}},
        "required": ["excerpt_id"],
    }
    return schema


def restore_evidence(raw, catalogue):
    result = copy.deepcopy(raw)
    for item in result.get("description_sections", []) + result.get("knowledge_points", []):
        restored = []
        for reference in item["evidence"]:
            if set(reference) != {"excerpt_id"} or reference["excerpt_id"] not in catalogue:
                raise ValueError("unknown or invalid evidence excerpt_id")
            restored.append(dict(catalogue[reference["excerpt_id"]]))
        item["evidence"] = restored
    return result


class ChapterBuilder:
    def __init__(self, client):
        self.client = client
        self.config = client.config
        self.diagnostics = []
        self.source_parts = {}
        self.background = {}

    def register_parts(self, records):
        for record in records:
            key = record["part_id"]
            if key in self.source_parts and self.source_parts[key] != record:
                raise ValueError("source part identity collision")
            self.source_parts[key] = record
        save(self.client.run_dir / "records.json", list(self.source_parts.values()))

    def payload(self, records, **extra):
        return {"events": records, "course_background": self.background, **extra}

    def prepare(self, events, visual_additions=None):
        def fit(record):
            # Reserve room for fixed chapter metadata and repair messages.
            budget = self.config.input_tokens - 256
            if self.client.fits(CONTENT_PROMPT, self.payload([record]), input_limit=budget):
                return [record]
            a, b = split_record(record)
            return fit(a) + fit(b)

        records = []
        for event in events:
            record = record_from_event(event)
            if visual_additions and event.event_id in visual_additions:
                record["visual_review"] = visual_additions[event.event_id]
            records.extend(fit(record))
        self.register_parts(records)
        return records

    def pack(self, records, prompt=GROUP_PROMPT, overlap=True):
        chunks, start = [], 0
        output = (
            self.config.boundary_output_tokens
            if prompt == GROUP_PROMPT
            else self.config.output_tokens
        )
        while start < len(records):
            end = start
            while end < len(records) and self.client.fits(
                prompt, self.payload(records[start : end + 1]), output
            ):
                end += 1
            if end == start:
                raise InputTooLong("prepared event cannot fit task prompt")
            chunks.append(records[start:end])
            if end == len(records):
                break
            next_start = end
            if overlap:
                parents = []
                for i in range(end - 1, start - 1, -1):
                    if records[i]["event_id"] not in parents:
                        parents.append(records[i]["event_id"])
                    if len(parents) > self.config.overlap_events:
                        break
                    next_start = i
                # At least one new record must fit on every iteration.
                while next_start < end and (
                    next_start <= start
                    or not self.client.fits(
                        prompt, self.payload(records[next_start : end + 1]), output
                    )
                ):
                    next_start += 1
                actual = len({r["event_id"] for r in records[next_start:end]})
                if actual < self.config.overlap_events:
                    self.diagnostics.append(
                        {
                            "kind": "overlap_reduced_by_budget",
                            "after_event": records[end - 1]["event_id"],
                            "actual": actual,
                        }
                    )
            start = next_start
        return chunks

    def validated(
        self,
        label,
        prompt,
        payload,
        validator,
        output=None,
        input_limit=None,
        schema=None,
        retry_truncated=False,
    ):
        self.client.response_schema = schema
        for attempt in range(self.config.retries + 1):
            try:
                raw = self.client.ask(label, prompt, payload, output, input_limit)
                return validator(raw)
            except InputTooLong:
                raise
            except OutputTooLong as exc:
                if not retry_truncated:
                    raise  # Chapter prose still uses the original recursive sectioning path.
                self.client.invalidate_last_response()
                if attempt == self.config.retries:
                    raise
                output = max(1, (output or self.config.output_tokens) // 2)
                prompt += "\n上次输出过长；逐行原样转录，每行只写一次，不纠错、不解释、不重复。"
                if not self.client.fits(prompt, payload, output, input_limit):
                    raise InputTooLong("repair prompt exceeds input budget") from exc
            except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError) as exc:
                self.client.invalidate_last_response()
                if attempt == self.config.retries:
                    raise
                # Keep the source unchanged. Only repair the failed output contract.
                prompt += "\n上次输出结构或引用不合法，请重新生成。错误：" + str(exc)[:180]
                if not self.client.fits(prompt, payload, output, input_limit):
                    raise InputTooLong("repair prompt exceeds input budget") from exc

    def partition(self, records):
        chunks = self.pack(records)
        candidates, votes, titles = [], {}, {}
        for i, chunk in enumerate(chunks):
            lo, hi = chunk[0]["event_id"], chunk[-1]["event_id"]

            def check(raw, lo=lo, hi=hi):
                groups = [Group.model_validate(g) for g in raw["chapters"]]
                validate_groups(groups, lo, hi)
                return groups

            groups = self.validated(
                f"group_{i}",
                GROUP_PROMPT,
                self.payload(chunk),
                check,
                self.config.boundary_output_tokens,
                schema=GroupResponse.model_json_schema(),
            )
            candidates.append([g.model_dump() for g in groups])
            proposed = {g.first_event: g.title for g in groups}
            for gap in range(lo + 1, hi + 1):
                centrality = min(gap - lo, hi - gap + 1)
                if gap not in votes or centrality > votes[gap][0]:
                    votes[gap] = (centrality, gap in proposed)
                    if gap in proposed:
                        titles[gap] = proposed[gap]
        boundaries = {gap for gap, (_, keep) in votes.items() if keep}
        titles[records[0]["event_id"]] = candidates[0][0]["title"]
        seams = []
        for i in range(1, len(chunks)):
            # Re-read the overlapping window, bounded exactly like any other request.
            window = chunks[i]
            previous = chunks[i - 1][-1]
            if window[0]["event_id"] > previous["event_id"]:
                lo = window[0]["event_id"]
                boundaries.add(lo)
                titles[lo] = candidates[i][0]["title"]
                bridge = [previous, window[0]]
                if not self.client.fits(
                    SEAM_PROMPT, self.payload(bridge), self.config.boundary_output_tokens
                ):
                    self.diagnostics.append(
                        {"kind": "seam_unresolved_input_budget", "before_event": lo}
                    )
                    raise InputTooLong("adjacent evidence cannot fit seam review; not publishable")
                window = bridge
            lo, hi = window[0]["event_id"], window[-1]["event_id"]
            data = self.payload(window, candidates=candidates[i])
            while not self.client.fits(SEAM_PROMPT, data, self.config.boundary_output_tokens):
                # Candidate rationale is optional context; raw evidence stays intact.
                data = self.payload(window)
                if not self.client.fits(SEAM_PROMPT, data, self.config.boundary_output_tokens):
                    raise InputTooLong("seam window exceeds budget")

            def check_seam(raw, lo=lo, hi=hi):
                result = raw["boundaries"]
                seen = set()
                for boundary in result:
                    eid = boundary["event_id"]
                    if type(eid) is not int or not lo < eid <= hi or eid in seen:
                        raise ValueError("invalid seam boundary")
                    if not isinstance(boundary["title"], str) or not boundary["title"].strip():
                        raise ValueError("missing seam title")
                    seen.add(eid)
                return result

            seam_schema = BoundaryResponse.model_json_schema()
            if lo == hi:
                seam_schema["properties"]["boundaries"]["maxItems"] = 0
            else:
                seam_schema["$defs"]["Boundary"]["properties"]["event_id"]["enum"] = list(
                    range(lo + 1, hi + 1)
                )
            result = self.validated(
                f"seam_{i}",
                SEAM_PROMPT,
                data,
                check_seam,
                self.config.boundary_output_tokens,
                schema=seam_schema,
            )
            boundaries.difference_update(range(lo + 1, hi + 1))
            for boundary in result:
                boundaries.add(boundary["event_id"])
                titles[boundary["event_id"]] = boundary["title"]
            seams.append({"window": [lo, hi], "boundaries": result})
        starts = [records[0]["event_id"], *sorted(boundaries)]
        end = records[-1]["event_id"]
        groups = [
            Group(
                first_event=start,
                last_event=starts[i + 1] - 1 if i + 1 < len(starts) else end,
                title=titles.get(start, "章节"),
                rationale="分块候选与接缝证据归并",
            )
            for i, start in enumerate(starts)
        ]
        validate_groups(groups, starts[0], end)
        save(
            self.client.run_dir / "partition.json",
            {
                "chunks": [[r["part_id"] for r in ch] for ch in chunks],
                "candidates": candidates,
                "seams": seams,
                "groups": [g.model_dump() for g in groups],
                "diagnostics": self.diagnostics,
            },
        )
        return groups

    def _content(self, label, prompt, records, **extra):
        wire, catalogue = evidence_catalogue(records)

        def check(raw):
            content = Content.model_validate(restore_evidence(raw, catalogue))
            fallback_unsupported_expansions(content, records)
            validate_content(content, records)
            review_content_quality(content, extra.get("chapter_position"))
            return content

        return self.validated(
            label,
            prompt,
            self.payload(wire, **extra),
            check,
            schema=selection_schema(Content, catalogue),
        )

    def content(self, label, records, title, variant="B", position=None):
        self.register_parts(records)
        extra = {"chapter_title": title}
        if position is not None:
            extra["chapter_position"] = position
        prompt = CONTENT_PROMPT
        try:
            if variant == "C":
                facts = self._content(label + "_facts", FACT_PROMPT, records, **extra)
                extra["knowledge_draft"] = facts.model_dump()
                prompt = WRITE_PROMPT
            return self._content(label, prompt, records, **extra)
        except (InputTooLong, OutputTooLong):
            # Expanded chapter: facts -> local outline -> raw-evidence section writing.
            if len(records) == 1:
                a, b = split_record(records[0])
                records = [a, b]
                self.register_parts(records)
            midpoint = len(records) // 2
            pieces = [records[:midpoint], records[midpoint:]]
            contents = []
            for i, part in enumerate(pieces):
                # Each leaf has bounded raw evidence. Fact sections are the local outline.
                contents.append(
                    self.content(
                        label + f"_section{i}", part, title, variant="C", position=position
                    )
                )
            points, seen = [], set()
            for content in contents:
                for point in content.knowledge_points:
                    signature = dumps(point.model_dump(exclude={"evidence"}))
                    if signature in seen:
                        previous = next(
                            p
                            for p in points
                            if dumps(p.model_dump(exclude={"evidence"})) == signature
                        )
                        for ref in point.evidence:
                            if ref not in previous.evidence:
                                previous.evidence.append(ref)
                    else:
                        points.append(point)
                        seen.add(signature)
            save(
                self.client.run_dir / "outlines" / f"{label}.json",
                {
                    "title": title,
                    "sections": [
                        {"title": s.title, "event_ids": sorted({e.event_id for e in s.evidence})}
                        for c in contents
                        for s in c.description_sections
                    ],
                },
            )
            return Content(
                title=title,
                knowledge_points=points,
                description_sections=[s for c in contents for s in c.description_sections],
                review_issues=list(
                    dict.fromkeys(
                        ["超长章节按原始顺序分节生成"]
                        + [x for c in contents for x in c.review_issues]
                    )
                ),
            )

    def materialize(self, group, content, events, index, duration):
        selected = events[group.first_event - 1 : group.last_event]
        if duration < selected[0].start_seconds or selected[-1].end_seconds - duration > 0.02:
            raise ValueError("source events exceed supplied video duration")
        return Chapter(
            **content.model_dump(),
            chapter_id=f"chapter_{index:03d}",
            first_event=group.first_event,
            last_event=group.last_event,
            start_seconds=selected[0].start_seconds,
            end_seconds=min(selected[-1].end_seconds, duration),
            source_event_ids=[e.event_id for e in selected],
            source_references=[
                {
                    "segment_id": e.segment_id,
                    "time_range": [e.start_seconds, e.end_seconds],
                }
                for e in selected
            ],
        )

    def generate(self, events, records, groups, duration, variant="B"):
        validate_groups(groups, 1, len(events))
        chapters = []
        for i, group in enumerate(groups, 1):
            selected = [
                r for r in records if group.first_event <= r["event_id"] <= group.last_event
            ]
            content = self.content(
                f"{variant}_chapter_{i}",
                selected,
                group.title,
                variant,
                position={
                    "chapter_index": i,
                    "chapter_count": len(groups),
                    "first_event": group.first_event,
                    "last_event": group.last_event,
                    "video_event_count": len(events),
                },
            )
            validate_content(
                content,
                [
                    r
                    for r in self.source_parts.values()
                    if group.first_event <= r["event_id"] <= group.last_event
                ],
            )
            chapters.append(self.materialize(group, content, events, i, duration))
            save(
                self.client.run_dir / f"{variant}.partial.json", [c.model_dump() for c in chapters]
            )
        return chapters

    def joint(self, events, records, duration):
        prompt = (
            GROUP_PROMPT.split("返回")[0]
            + "\n本次为全视频同时生成多个语义章节及每章的三部分内容。"
            + CONTENT_PROMPT.replace("章节范围固定。", "").replace(
                "根据本章原始events", "对每个划定章节的events"
            )
            + '\n最终顶层返回{"chapters":[每章包含first_event,last_event和上述内容字段]}。'
            + "所有章节合起来每个event恰好一次，完整覆盖全视频。"
        )
        limit = self.config.context_tokens - self.config.output_tokens - self.config.safety_tokens
        wire, catalogue = evidence_catalogue(records)

        def check(raw):
            chapters, groups = [], []
            for i, chapter in enumerate(raw["chapters"], 1):
                data = dict(chapter)
                lo, hi = data.pop("first_event"), data.pop("last_event")
                group = Group(first_event=lo, last_event=hi, title=data["title"])
                content = Content.model_validate(restore_evidence(data, catalogue))
                chapter_records = [r for r in records if lo <= r["event_id"] <= hi]
                fallback_unsupported_expansions(content, chapter_records)
                validate_content(content, chapter_records)
                review_content_quality(
                    content, {"chapter_index": i, "chapter_count": len(raw["chapters"])}
                )
                groups.append(group)
                chapters.append(self.materialize(group, content, events, i, duration))
            validate_groups(groups, 1, len(events))
            return chapters

        return self.validated(
            "A_joint",
            prompt,
            self.payload(wire),
            check,
            input_limit=limit,
            schema=selection_schema(JointResponse, catalogue),
        )


def provenance(payload, config):
    return {
        "source_sha256": hashlib.sha256(dumps(payload).encode()).hexdigest(),
        "prompt_version": VERSION,
        "model": config.model,
        "config": config.model_dump(mode="json"),
        "grounding": "indexed_evidence; quote checks are not semantic verification",
    }


def run_demo(
    payload, duration, client, variants=("A", "B", "C"), partition_only=False, video_path=None
):
    metadata = provenance(payload, client.config)
    manifest = client.run_dir / "run_manifest.json"
    prior = manifest if manifest.exists() else client.run_dir / "experiment.json"
    if prior.exists():
        previous = json.loads(prior.read_text())
        if any(previous.get(key) != value for key, value in metadata.items()):
            raise ValueError("run source/config/prompt changed; choose a new output directory")
    save(manifest, metadata)
    events = extract_events(payload)
    builder = ChapterBuilder(client)
    visual_additions = None
    if client.config.visual_review:
        from .visual import review_visual_events

        if video_path is None:
            raise ValueError("visual review requires video_path")
        visual_additions = review_visual_events(events, video_path, client)
    records = builder.prepare(events, visual_additions)
    save(client.run_dir / "events.json", [e.model_dump() for e in events])
    save(client.run_dir / "records.json", records)
    groups = builder.partition(records)
    results = {}
    if not partition_only:
        for variant in variants:
            result_path = client.run_dir / f"{variant}.chapters.json"
            if result_path.exists():
                # Preserve historical output without leaving a stale completed publication.
                result_path.rename(client.run_dir / f"{variant}.previous.{uuid.uuid4().hex}.json")
            try:
                chapters = (
                    builder.joint(events, records, duration)
                    if variant == "A"
                    else builder.generate(events, records, groups, duration, variant)
                )
                result = {
                    **provenance(payload, client.config),
                    "status": "completed",
                    "variant": variant,
                    "chapters": [c.model_dump() for c in chapters],
                }
                save(client.run_dir / f"{variant}.chapters.json", result)
                results[variant] = {
                    "status": "completed",
                    "chapters": len(chapters),
                    "knowledge_points": sum(len(c.knowledge_points) for c in chapters),
                }
            except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError) as exc:
                # A rejected variant must not discard already validated B/C artifacts.
                # Partial output is retained for diagnosis, never published as completed.
                results[variant] = {
                    "status": "skipped"
                    if variant == "A" and isinstance(exc, InputTooLong)
                    else "failed",
                    "reason": type(exc).__name__,
                    "error": str(exc)[:400],
                }
    summary = {
        **provenance(payload, client.config),
        "event_count": len(events),
        "chapter_count": len(groups),
        "results": results,
        "diagnostics": builder.diagnostics,
        "calls": client.calls,
    }
    save(client.run_dir / "experiment.json", summary)
    return summary
