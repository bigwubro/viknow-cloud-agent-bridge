"""Explicit learning jobs with versioned evidence and one source-adjacent public file."""

import asyncio
import fcntl
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from pydantic import TypeAdapter

from .builder import VERSION, ChapterBuilder, provenance
from .client import ModelClient, dumps, save
from .domain import Chapter, ChapterConfig, extract_events
from .export import LearningChapter, export_chapters
from .outline import (
    build_outline,
    extract_outline,
    generate_timed_chapters,
    make_spans,
    summarize_inferred_parents,
)
from .reuse import seed_raw_cache
from .title_conflicts import apply_title_results, reconcile_titles


def fingerprint(payload):
    return hashlib.sha256(dumps(payload).encode()).hexdigest()


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class ChapterJobs:
    def __init__(self, root: Path, config, source, client_factory=ModelClient):
        self.root, self.config, self.source = root, config, source
        self.client_factory = client_factory
        self.running, self.locks = {}, {}
        self.closing = False
        self.semaphore = asyncio.Semaphore(1)

    def asset_dir(self, workspace, asset_id):
        return self.root / fingerprint([workspace, asset_id])

    def _latest(self, base):
        path = base / "latest.json"
        return json.loads(path.read_text())["job_key"] if path.exists() else None

    def _check_source(self, request):
        payload = json.loads(Path(request["sop_path"]).read_text())
        if fingerprint(payload) != request["source_sha256"]:
            raise ValueError("semantic source changed; request learning generation again")

    def _publish(self, run_dir, request, result):
        chapters = [Chapter.model_validate(c) for c in result["chapters"]]
        payload = json.loads((run_dir / "source.json").read_text())
        events = extract_events(payload)
        previous = 0.0
        for chapter in chapters:
            ids = [
                e.event_id
                for e in events
                if e.start_seconds < chapter.end_seconds and e.end_seconds > chapter.start_seconds
            ]
            if chapter.source_event_ids != ids or abs(chapter.start_seconds - previous) > 0.01:
                raise ValueError(
                    "chapter time coverage or overlapping source assignment is invalid"
                )
            previous = chapter.end_seconds
        if abs(previous - request["duration"]) > 0.02:
            raise ValueError("incomplete chapter export")
        exported = [
            c.model_dump(mode="json")
            for c in export_chapters(chapters, request["asset_id"], request["file_title"])
        ]
        titles = result.get("inferred_parent_titles")
        if request["prompt_version"] == VERSION and not isinstance(titles, dict):
            raise ValueError("current chapter result requires inferred parent titles")
        outline = build_outline(
            result.get("title_observations", []),
            chapters,
            request["source_sha256"],
            "",
            titles,
        )
        with file_lock(run_dir.parent / "publication.lock"):
            if self._latest(run_dir.parent) != request["job_key"]:
                raise ValueError("learning request superseded by a newer submission")
            self._check_source(request)
            target = Path(request["sop_path"]).with_name("charpter.json")
            save(run_dir / "charpter.json", exported)
            request["export_sha256"] = hashlib.sha256(
                (run_dir / "charpter.json").read_bytes()
            ).hexdigest()
            outline["chapter_version"] = request["export_sha256"]
            save(run_dir / "slide-outline.json", outline)
            request["outline_sha256"] = hashlib.sha256(
                (run_dir / "slide-outline.json").read_bytes()
            ).hexdigest()
            self._archive_translation(run_dir, request)
            save(target, exported)
            save(target.with_name("slide-outline.json"), outline)
            request["status"], request["phase"] = "completed", "completed"
            save(run_dir / "job.json", request)
            if not request.get("defer_publication"):
                save(run_dir.parent / "published.json", {"job_key": request["job_key"]})
        return exported

    async def enqueue(
        self, workspace, asset_id, retry=False, *, snapshot=None, publish_course=True
    ):
        if self.closing:
            raise RuntimeError("chapter service is closing")
        async with self.locks.setdefault((workspace, asset_id), asyncio.Lock()):
            if snapshot is not None:
                status, payload = snapshot.read()
            else:
                status = await self.source.aget_asset_status(workspace=workspace, asset_id=asset_id)
            if status is None:
                raise KeyError("asset not found")
            state = getattr(status, "status", None)
            if str(getattr(state, "value", state)) != "completed":
                raise ValueError("video parsing/indexing is not completed")
            if snapshot is None:
                payload = await self.source.aget_video_semantic(workspace, asset_id)
            if payload is None:
                raise ValueError("completed video semantic artifact not available")
            if self.closing:
                raise RuntimeError("chapter service is closing")
            raw_path = getattr(status, "sop_path", None)
            if not raw_path:
                raise ValueError("canonical semantic artifact path is unavailable")
            sop = Path(raw_path).resolve()
            if sop.name != "sop-full.json" or not sop.is_file():
                raise ValueError("canonical sop-full.json is unavailable")
            if fingerprint(json.loads(sop.read_text())) != fingerprint(payload):
                raise ValueError("semantic payload and source file are not the same snapshot")
            events = extract_events(payload)
            if {e.video_id for e in events} != {asset_id}:
                raise ValueError("one learning artifact requires one source video")
            file_title = (
                getattr(status, "file_title", None)
                or (payload.get("metadata") or {}).get("title")
                or asset_id
            )
            duration = getattr(status, "duration_seconds", None) or events[-1].end_seconds
            metadata = provenance(payload, self.config)
            background_path = sop.with_name("course-background.json")
            background = json.loads(background_path.read_text()) if background_path.exists() else {}
            metadata["background_sha256"] = fingerprint(background)
            metadata["outline_requested"] = background_path.exists()
            metadata["defer_publication"] = not publish_course
            key = fingerprint([workspace, asset_id, str(sop), file_title, duration, metadata])
            base = self.asset_dir(workspace, asset_id)
            run_dir = base / key
            job_path = run_dir / "job.json"
            should_start = False
            with file_lock(base / "publication.lock"):
                self._preserve_published(base)
                request = json.loads(job_path.read_text()) if job_path.exists() else None
                if request is None or (request["status"] == "failed" and retry):
                    request = {
                        "workspace": workspace,
                        "asset_id": asset_id,
                        "job_key": key,
                        "status": "queued",
                        "source_sha256": metadata["source_sha256"],
                        "sop_path": str(sop),
                        "file_title": str(file_title),
                        "duration": duration,
                        "video_path": str(getattr(status, "file_path", "") or ""),
                        "config": self.config.model_dump(mode="json"),
                        "prompt_version": VERSION,
                        "phase": "queued",
                        "background": background,
                        "outline_requested": background_path.exists(),
                        "defer_publication": not publish_course,
                        "error": None,
                    }
                    save(run_dir / "source.json", payload)
                    save(job_path, request)
                should_start = request["status"] in {"queued", "running"}
                # Commit the pointer only after its complete request snapshot exists.
                save(base / "latest.json", {"job_key": key})
            if request["status"] == "completed":
                try:
                    self._validate_run_artifacts(run_dir, request)
                except (OSError, ValueError):
                    if not retry:
                        return self.view(workspace, asset_id)
                    # Regenerate derived files from the verified source and cached
                    # model calls, without resubmitting the original index job.
                    request.update(status="queued", phase="queued", error=None)
                    with file_lock(base / "publication.lock"):
                        save(run_dir / "source.json", payload)
                        save(job_path, request)
                    self._start(run_dir, request)
                    return self.view(workspace, asset_id)
                if self.view(workspace, asset_id)["status"] == "stale":
                    result = json.loads((run_dir / "chapters.json").read_text())
                    self._publish(run_dir, request, result)
                return self.view(workspace, asset_id)
            if should_start:
                # Also resumes an orphan when the feature was enabled after startup.
                # generation.lock makes this safe across multiple managers/workers.
                self._start(run_dir, request)
            return self.view(workspace, asset_id)

    def _start(self, run_dir, request):
        key = request["job_key"]
        if key not in self.running:
            task = asyncio.create_task(self._run(run_dir, request))
            self.running[key] = task
            task.add_done_callback(lambda done: self.running.pop(key, None))

    async def _run(self, run_dir, request):
        async with self.semaphore:
            await asyncio.to_thread(self._work, run_dir, request)

    def _work(self, run_dir, request):
        with file_lock(run_dir / "generation.lock"):
            persisted = json.loads((run_dir / "job.json").read_text())
            if persisted["status"] in {"completed", "failed"}:
                return
            request = persisted
            request["status"] = "running"
            with file_lock(run_dir.parent / "publication.lock"):
                save(run_dir / "job.json", request)
            try:
                self._check_source(request)
                if request["prompt_version"] != VERSION:
                    raise ValueError("generator version changed; resubmit the learning request")
                # Recovery uses the original explicit request, not current service defaults.
                config = ChapterConfig.model_validate(request["config"])
                payload = json.loads((run_dir / "source.json").read_text())
                try:
                    pointer = run_dir.parent / "published.json"
                    if pointer.exists():
                        previous = run_dir.parent / json.loads(pointer.read_text())["job_key"]
                        if previous != run_dir and (previous / "job.json").exists():
                            prior = json.loads((previous / "job.json").read_text())
                            self._validate_run_artifacts(previous, prior)
                            seed_raw_cache(previous, run_dir, request)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    # Optional prior cache never masks validation of this run's source.
                    request["cache_reuse_warning"] = str(exc)
                client = self.client_factory(config, run_dir)
                client.verify_model()
                builder = ChapterBuilder(client)
                builder.background = request.get("background", {})
                events = extract_events(payload)
                additions = None
                if config.visual_review:
                    from .visual import review_visual_events

                    additions = review_visual_events(events, Path(request["video_path"]), client)
                records = builder.prepare(events, additions)
                save(run_dir / "records.json", records)
                observations, spans = [], []
                if request.get("outline_requested"):
                    request["phase"] = "titles"
                    save(run_dir / "job.json", request)
                    observations = extract_outline(
                        events,
                        Path(request["video_path"]),
                        request["duration"],
                        client,
                        builder.background,
                    )
                    observations = reconcile_titles(
                        observations, Path(request["video_path"]), builder
                    )
                    spans = make_spans(observations, request["duration"])
                request["phase"] = "chapters"
                save(run_dir / "job.json", request)
                if spans:
                    from ..translations import transcript_input

                    chapters = generate_timed_chapters(
                        builder,
                        events,
                        records,
                        spans,
                        request["duration"],
                        video_path=Path(request["video_path"]),
                        observations=observations,
                        segments=transcript_input(payload, request["asset_id"])[0],
                    )
                else:
                    groups = builder.partition(records)
                    chapters = builder.generate(events, records, groups, request["duration"], "B")
                if observations:
                    # Fresh body-region evidence may expose a conflict absent in title OCR.
                    observations = reconcile_titles(
                        observations, Path(request["video_path"]), builder
                    )
                    apply_title_results(chapters, observations, request["duration"])
                request["phase"] = "outline_summaries"
                save(run_dir / "job.json", request)
                inferred_parent_titles = summarize_inferred_parents(observations, chapters, builder)
                result = {
                    **provenance(payload, config),
                    "chapters": [c.model_dump() for c in chapters],
                    "diagnostics": builder.diagnostics,
                    "title_observations": observations,
                    "inferred_parent_titles": inferred_parent_titles,
                }
                save(run_dir / "chapters.json", result)
                request["phase"] = "publishing"
                save(run_dir / "job.json", request)
                self._publish(run_dir, request, result)
                request["status"] = "completed"
            except Exception as exc:
                request["status"] = "failed"
                request["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
            with file_lock(run_dir.parent / "publication.lock"):
                save(run_dir / "job.json", request)

    async def recover(self):
        # No index watches. Only queued/running requests already explicitly submitted.
        for path in self.root.glob("*/*/job.json"):
            should_start = False
            with file_lock(path.parent.parent / "publication.lock"):
                request = json.loads(path.read_text())
                if request["status"] in {"queued", "running"}:
                    if self._latest(path.parent.parent) != request["job_key"]:
                        request.update(status="failed", error="superseded learning request")
                        save(path, request)
                    else:
                        should_start = True
            if should_start:
                self._start(path.parent, request)

    def _validate_run_artifacts(self, run_dir, job):
        if fingerprint(json.loads((run_dir / "source.json").read_text())) != job["source_sha256"]:
            raise ValueError("chapter source snapshot changed")
        for name, key in (
            ("charpter.json", "export_sha256"),
            ("slide-outline.json", "outline_sha256"),
        ):
            if not job.get(key):
                continue
            path = run_dir / name
            # Legacy generations predate run-local exported files.
            if name == "charpter.json" and not path.exists() and job["prompt_version"] != VERSION:
                path = Path(job["sop_path"]).with_name(name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != job[key]:
                raise ValueError(f"chapter artifact changed: {name}")

    def view(self, workspace, asset_id, *, require_current_config=True):
        """Validate published artifacts; generation callers also require current settings."""
        base = self.asset_dir(workspace, asset_id)
        key = self._latest(base)
        if key is None:
            return {"status": "not_generated", "job_key": None, "chapters": [], "error": None}
        run_dir = base / key
        path = run_dir / "job.json"
        if not path.exists():
            return {"status": "queued", "job_key": key, "chapters": [], "error": None}
        job = json.loads(path.read_text())
        result = {
            "status": job["status"],
            "job_key": key,
            "source_sha256": job["source_sha256"],
            "chapters": [],
            "error": job.get("error"),
            "phase": job.get("phase", job["status"]),
        }
        if job["status"] == "completed":
            try:
                self._check_source(job)
                self._validate_run_artifacts(run_dir, job)
                if require_current_config and (
                    job["config"] != self.config.model_dump(mode="json")
                    or job["prompt_version"] != VERSION
                ):
                    raise ValueError("learning configuration changed")
                raw = Path(job["sop_path"]).with_name("charpter.json").read_bytes()
                if hashlib.sha256(raw).hexdigest() != job.get("export_sha256"):
                    raise ValueError("learning sidecar changed")
                data = TypeAdapter(list[LearningChapter]).validate_json(raw)
                result["chapters"] = [c.model_dump(mode="json") for c in data]
            except (OSError, ValueError) as exc:
                result.update(status="stale", error=str(exc))
        return result

    def _preserve_published(self, base):
        """Migrate existing successful publication before changing the latest request."""
        pointer = base / "published.json"
        key = json.loads(pointer.read_text())["job_key"] if pointer.exists() else self._latest(base)
        if key is None:
            return
        run_dir = base / key
        job = json.loads((run_dir / "job.json").read_text())
        if job["status"] != "completed":
            return
        target = Path(job["sop_path"]).with_name("charpter.json")
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != job.get(
            "export_sha256"
        ):
            return
        if not (run_dir / "charpter.json").exists():
            (run_dir / "charpter.json").write_bytes(target.read_bytes())
        self._archive_translation(run_dir, job)
        outline = target.with_name("slide-outline.json")
        if outline.exists() and not (run_dir / "slide-outline.json").exists():
            data = json.loads(outline.read_text())
            if data.get("chapter_version") == job["export_sha256"]:
                save(run_dir / "slide-outline.json", data)
        save(base / "published.json", {"job_key": key})

    def _archive_translation(self, run_dir, job):
        path = Path(job["sop_path"]).with_name("reading-plan.json")
        if not path.exists():
            return
        from ..translations import transcript_input

        _, version = transcript_input(
            json.loads((run_dir / "source.json").read_text()), job["asset_id"]
        )
        plan = json.loads(path.read_text())
        if plan.get("asset_id") == job["asset_id"] and plan.get("transcript_version") == version:
            save(run_dir / "reading-plan.json", plan)

    def published_view(self, workspace, asset_id):
        """Return the immutable last successful course, regardless of a newer attempt."""
        base = self.asset_dir(workspace, asset_id)
        pointer = base / "published.json"
        key = json.loads(pointer.read_text())["job_key"] if pointer.exists() else self._latest(base)
        if key is None:
            return {"status": "not_generated", "job_key": None, "chapters": [], "error": None}
        run_dir = base / key
        job = json.loads((run_dir / "job.json").read_text())
        if job["status"] != "completed":
            return {
                "status": job["status"],
                "job_key": key,
                "chapters": [],
                "error": job.get("error"),
            }
        source = run_dir / "source.json"
        if fingerprint(json.loads(source.read_text())) != job["source_sha256"]:
            raise ValueError("published source snapshot changed")
        chapter_path = run_dir / "charpter.json"
        if not chapter_path.exists():
            chapter_path = Path(job["sop_path"]).with_name("charpter.json")
        raw = chapter_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != job.get("export_sha256"):
            raise ValueError("published chapter snapshot changed")
        chapters = TypeAdapter(list[LearningChapter]).validate_json(raw)
        outline_path = run_dir / "slide-outline.json"
        outline = json.loads(outline_path.read_text()) if outline_path.exists() else None
        if job.get("outline_sha256") and (
            not outline_path.exists()
            or hashlib.sha256(outline_path.read_bytes()).hexdigest() != job["outline_sha256"]
        ):
            raise ValueError("published outline snapshot changed")
        if outline and (
            outline["chapter_version"] != job["export_sha256"]
            or outline["source_sha256"] != job["source_sha256"]
        ):
            raise ValueError("published outline version mismatch")
        return {
            "status": "completed",
            "phase": "completed",
            "job_key": key,
            "source_sha256": job["source_sha256"],
            "export_sha256": job["export_sha256"],
            "chapters": [c.model_dump(mode="json") for c in chapters],
            "outline": outline,
            "error": None,
        }

    async def wait_idle(self):
        if self.running:
            await asyncio.gather(*list(self.running.values()))

    async def close(self):
        self.closing = True
        await self.wait_idle()
