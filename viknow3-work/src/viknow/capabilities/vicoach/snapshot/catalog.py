"""Read completed learning artifacts as selectable courses; never start generation."""

import json
import logging
import re
from pathlib import Path

from pydantic import Field

from ..chapters.domain import extract_events
from ..chapters.jobs import ChapterJobs, fingerprint
from ..chapters.topics import needs_topic_grouping, read_topics
from ..translations import decorate_lesson
from .models import DescriptionSection, Model
from .service import build_lesson, load_lesson

logger = logging.getLogger(__name__)


def read_description_sections(run_dir, chapters):
    """Optional presentation structure must reproduce the verified public text exactly."""
    path = run_dir / "chapters.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())["chapters"]
        if [c["chapter_id"] for c in raw] != [c["chapter_id"] for c in chapters]:
            raise ValueError("description chapter identities differ")
        result = {}
        for source, published in zip(raw, chapters, strict=True):
            sections = [
                DescriptionSection(title=s["title"], body=s["body"])
                for s in source["description_sections"]
            ]
            text = "\n\n".join(f"{s.title}\n{s.body}" for s in sections)
            if text != published["description"]:
                raise ValueError("description structure differs from published text")
            result[published["chapter_id"]] = [s.model_dump() for s in sections]
        return result
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Description structure unavailable; keeping verified chapter text")
        return {}


class CourseSummary(Model):
    course_id: str
    library_id: str | None = None
    title: str
    duration_seconds: float
    chapter_count: int = Field(gt=0)
    is_sample: bool = False
    processing: dict | None = None


class CourseCatalog:
    def __init__(self, bundle: Path, jobs: ChapterJobs | None = None, courses=None):
        self.bundle, self.jobs, self.course_service = bundle, jobs, courses

    def _entry(self, course_id):
        if self.jobs is None or not re.fullmatch(r"[a-f0-9]{64}", course_id):
            raise KeyError("course not found")
        base = self.jobs.root / course_id
        pointer = base / "published.json"
        key = (
            json.loads(pointer.read_text())["job_key"]
            if pointer.exists()
            else self.jobs._latest(base)
        )
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise KeyError("course not found")
        job = json.loads((base / key / "job.json").read_text())
        if fingerprint([job["workspace"], job["asset_id"]]) != course_id:
            raise ValueError("course identity mismatch")
        result = self.jobs.published_view(job["workspace"], job["asset_id"])
        if result["status"] != "completed" or result["job_key"] != key:
            raise ValueError("course is no longer ready")
        if not Path(job["video_path"]).is_file():
            raise ValueError("course video is unavailable")
        return job, result

    def preparation_source(self, course_id):
        """Resolve trusted course artifacts; never accept client file paths."""
        if course_id == "sample":
            lesson = load_lesson(self.bundle)  # Verifies every bundled checksum first.
            manifest = json.loads((self.bundle / "manifest.json").read_text())
            payload = json.loads((self.bundle / "sop-full.json").read_text())
            if "transcript.json" in manifest["sha256"]:
                payload["transcript"] = json.loads((self.bundle / "transcript.json").read_text())
            return {
                "workspace": "coach-sample",
                "asset_id": lesson.asset_id,
                "title": lesson.sample,
                "duration": lesson.duration_seconds,
                "video_path": str(self.bundle / "video.mp4"),
                "payload": payload,
                "background": {},
            }
        job, _ = self._entry(course_id)
        _, reading_plan = self._translation(course_id, job, self.lesson(course_id))
        run_dir = self.jobs.root / course_id / job["job_key"]
        return {
            "workspace": job["workspace"],
            "asset_id": job["asset_id"],
            "title": job["file_title"],
            "duration": job["duration"],
            "video_path": job["video_path"],
            "payload": json.loads((run_dir / "source.json").read_text()),
            "background": job.get("background", {}),
            "chapter_job_key": job["job_key"],
            "reading_plan": reading_plan,
        }

    def _translation(self, course_id, job, lesson):
        run_dir = self.jobs.root / course_id / job["job_key"]
        for path in (
            run_dir / "reading-plan.json",
            Path(job["sop_path"]).with_name("reading-plan.json"),
        ):
            try:
                plan = json.loads(path.read_text())
                decorate_lesson(lesson, plan)  # Choose only a source-aligned artifact.
                return path, plan
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None, None

    def lesson(self, course_id):
        if course_id == "sample":
            path = self.bundle.parent / "public-health15.reading-plan.json"
            plan = json.loads(path.read_text()) if path.is_file() else None
            lesson = decorate_lesson(load_lesson(self.bundle), plan)
            if self.jobs is not None and len(lesson.chapters) > 1:
                source = self.preparation_source("sample")
                outline = read_topics(
                    self.jobs.root / "sample" / "semantic-outline.json",
                    [c.model_dump() for c in lesson.chapters],
                    asset_id=lesson.asset_id,
                    source_sha256=fingerprint(source["payload"]),
                    chapter_version=lesson.chapter_version,
                    background=source.get("background", {}),
                )
                if outline is not None:
                    lesson = lesson.model_copy(update={"outline": outline})
            return lesson
        job, result = self._entry(course_id)
        run_dir = self.jobs.root / course_id / job["job_key"]
        sop = json.loads((run_dir / "source.json").read_text())
        if fingerprint(sop) != result["source_sha256"]:
            raise ValueError("course source changed")
        events = [e.model_dump() for e in extract_events(sop)]
        sections = read_description_sections(run_dir, result["chapters"])
        chapters = []
        for c in result["chapters"]:
            ids = c["metadata"]["source_event_ids"]
            selected = [e for e in events if e["event_id"] in ids]
            chapters.append(
                {
                    "chapter_id": c["chapter_id"],
                    "title": c["title"],
                    "summary": c["description"],
                    "description_sections": sections.get(c["chapter_id"], []),
                    "start_seconds": c["metadata"]["location"]["start"],
                    "end_seconds": c["metadata"]["location"]["end"],
                    "first_event": ids[0],
                    "last_event": ids[-1],
                    "source_event_ids": ids,
                    "source_references": [
                        {
                            "segment_id": e["segment_id"],
                            "time_range": [e["start_seconds"], e["end_seconds"]],
                        }
                        for e in selected
                    ],
                }
            )
        lesson = build_lesson(
            manifest={
                "asset_id": job["asset_id"],
                "sample": job["file_title"],
                "duration_seconds": job["duration"],
            },
            sop=sop,
            events=events,
            chapters=chapters,
            chapter_version=job["export_sha256"],
        )

        _, plan = self._translation(course_id, job, lesson)
        if plan and (
            plan.get("asset_id") != lesson.asset_id
            or plan.get("transcript_version") != lesson.transcript_version
        ):
            archive = run_dir / "reading-plan.json"
            plan = json.loads(archive.read_text()) if archive.exists() else None
        lesson = decorate_lesson(lesson, plan)
        background_path = Path(job["sop_path"]).with_name("course-background.json")
        background = json.loads(background_path.read_text()) if background_path.is_file() else {}
        task = self.course_service.for_course(course_id) if self.course_service else None
        outline = result.get("outline")
        if needs_topic_grouping(lesson.chapters, outline):
            outline = (
                read_topics(
                    run_dir / "semantic-outline.json",
                    result["chapters"],
                    asset_id=job["asset_id"],
                    source_sha256=job["source_sha256"],
                    chapter_version=job["export_sha256"],
                    background=job.get("background", background),
                )
                or outline
            )
        return lesson.model_copy(
            update={
                "course_background": job.get("background", background),
                "outline": outline,
                "processing": task.model_dump(mode="json") if task else None,
            }
        )

    def courses(self):
        ids = []
        if self.jobs is not None:
            ids = sorted(p.parent.name for p in self.jobs.root.glob("*/latest.json"))
        ids.append("sample")
        result = []
        for course_id in ids:
            try:
                lesson = self.lesson(course_id)
                library_id = (
                    self._entry(course_id)[0]["workspace"] if course_id != "sample" else None
                )
                if course_id == "sample" and not (self.bundle / "video.mp4").is_file():
                    continue
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Unavailable Coach course %s (%s)", course_id, type(exc).__name__)
                continue  # A broken/stale course must not hide other completed courses.
            result.append(
                CourseSummary(
                    course_id=course_id,
                    library_id=library_id,
                    title=lesson.sample,
                    duration_seconds=lesson.duration_seconds,
                    chapter_count=len(lesson.chapters),
                    is_sample=course_id == "sample",
                    processing=lesson.processing,
                )
            )
        return result

    def resource(self, course_id, name):
        if name not in {"video.mp4", "reading-plan.json", "questions.json", "slide-outline.json"}:
            raise KeyError("resource not found")
        if course_id == "sample":
            self.lesson(course_id)
            return {
                "video.mp4": self.bundle / "video.mp4",
                "reading-plan.json": self.bundle.parent / "public-health15.reading-plan.json",
                "questions.json": self.bundle.parent / "questions.sample.json",
                "slide-outline.json": self.bundle / "slide-outline.json",
            }[name]
        job, _ = self._entry(course_id)
        if name == "slide-outline.json":
            return self.jobs.root / course_id / job["job_key"] / name
        if name == "reading-plan.json":
            lesson = self.lesson(course_id)
            path, _ = self._translation(course_id, job, lesson)
            if path is None:
                raise KeyError("no source-aligned translation")
            return path
        if name == "video.mp4":
            return Path(job["video_path"])
        path = Path(job["sop_path"]).with_name(name)
        # A question report belongs to one published chapter version. Missing
        # identity is not evidence that an old report is safe for a new course.
        report = json.loads(path.read_text())
        if not isinstance(report, dict):
            raise ValueError("question report must be an object")
        if (report.get("asset_id"), report.get("chapter_version")) != (
            job["asset_id"],
            job["export_sha256"],
        ):
            raise ValueError("questions do not match the published course version")
        return path
