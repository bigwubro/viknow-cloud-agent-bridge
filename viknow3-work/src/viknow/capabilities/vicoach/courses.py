"""Coach-owned durable preparation tasks, reusing upload and chapter services."""

import asyncio
import fcntl
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator

from viknow.core.knowledge.uploads.manager import resolve_upload_library_id

from .chapters.client import save
from .chapters.export import LearningChapter
from .chapters.jobs import ChapterJobs, file_lock, fingerprint
from .chapters.source import ChapterSnapshot, IndexedChapterSource
from .chapters.topics import generate_topics, needs_topic_grouping, read_topics
from .snapshot.models import Model
from .translations import (
    CourseBackground,
    MissingTranscript,
    sentences,
    transcript_input,
    translate,
    validate_plan,
)

State = Literal["pending", "running", "completed", "failed", "not_applicable"]


class CourseStages(Model):
    upload: State = "pending"
    normalization: State = "pending"
    indexing: State = "pending"
    titles: State = "pending"
    translation: State = "pending"
    chapters: State = "pending"
    topics: State = "pending"


class CourseTask(Model):
    task_id: str
    course_id: str
    library_id: str
    asset_id: str
    title: str
    background: CourseBackground
    status: Literal["queued", "processing", "ready", "failed"] = "queued"
    stages: CourseStages = Field(default_factory=CourseStages)
    error: str | None = None
    error_code: str | None = None
    retryable: bool = True
    created_at: str
    updated_at: str
    preview_url: str
    speech_status: str | None = None
    translation_quality: Literal["model_reviewed", "needs_review"] | None = None
    translation_review_count: int = Field(default=0, ge=0)
    segmentation_mode: Literal["general", "presentation"] = "general"


class TaskRecord(CourseTask):
    translation_attempt: int = Field(default=0, ge=0)
    source_path: str | None = None
    upload_id: str | None = None
    video_path: str | None = None
    sop_path: str | None = None
    source_sha256: str | None = None
    snapshot: ChapterSnapshot | None = None
    chapter_job_key: str | None = None


class AdoptCourse(Model):
    library_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    asset_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    background: CourseBackground = Field(default_factory=CourseBackground)

    @field_validator("library_id")
    @classmethod
    def valid_library(cls, value):
        if value in {".", ".."}:
            raise ValueError("invalid library identity")
        return value


class PrepareCourse(Model):
    background: CourseBackground | None = None


def now():
    return datetime.now(UTC).isoformat()


class CourseService:
    def __init__(
        self, config_provider, upload_provider, adapter_provider, chapters, translator=translate
    ):
        self.config_provider = config_provider
        self.upload_provider = upload_provider
        self.adapter_provider = adapter_provider
        self.chapters = chapters
        self.translator = translator
        self.topic_generator = generate_topics
        self.running = {}
        self.closing = False

    def settings(self):
        root, model = self.chapters.settings()
        return root.parent / "courses", self.config_provider().capabilities.vicoach.courses, model

    def path(self, task_id):
        task_id = str(UUID(task_id))
        return self.settings()[0] / task_id / "task.json"

    def read(self, task_id):
        return TaskRecord.model_validate_json(self.path(task_id).read_text())

    def write(self, record):
        record.updated_at = now()
        save(self.path(record.task_id), record.model_dump(mode="json"))

    @staticmethod
    def public(record):
        return CourseTask.model_validate(record.model_dump(include=set(CourseTask.model_fields)))

    def list(self):
        root, _, _ = self.settings()
        result = []
        for path in sorted(root.glob("*/task.json"), reverse=True):
            try:
                result.append(self.public(TaskRecord.model_validate_json(path.read_text())))
            except (OSError, ValueError):
                continue  # One corrupt task must not hide all other tasks.
        return sorted(result, key=lambda t: t.created_at, reverse=True)

    def for_course(self, course_id):
        return next((t for t in self.list() if t.course_id == course_id), None)

    def _new(self, library_id, asset_id, title, background, **extra):
        task_id = str(uuid4())
        record = TaskRecord(
            task_id=task_id,
            course_id=fingerprint([library_id, asset_id]),
            library_id=library_id,
            asset_id=asset_id,
            title=title,
            background=background,
            created_at=now(),
            updated_at=now(),
            preview_url=f"/coach/tasks/{task_id}/video.mp4",
            **extra,
        )
        self.write(record)
        return record

    async def upload(
        self,
        filename,
        stream,
        background,
        library_id=None,
        segmentation_mode: Literal["general", "presentation"] = "general",
    ):
        if not filename or Path(filename).suffix.lower() not in {
            ".mp4",
            ".mov",
            ".mkv",
            ".avi",
            ".webm",
            ".m4v",
            ".mpeg",
            ".mpg",
        }:
            raise ValueError("请选择支持的视频文件")
        config = self.config_provider()
        if config.knowledge is None:
            raise RuntimeError("课程上传需要配置知识库")
        library_id = resolve_upload_library_id(library_id, config.knowledge.default_library_id)
        asset_id = str(uuid4())
        record = self._new(
            library_id,
            asset_id,
            Path(filename).name,
            background,
            upload_id=asset_id,
            segmentation_mode=segmentation_mode,
        )
        source = self.path(record.task_id).parent / ("source" + Path(filename).suffix.lower())
        record.source_path = str(source)
        self.write(record)  # Context and identity exist before indexing can start.
        try:
            await asyncio.to_thread(self._copy_upload, source, stream)
            if source.stat().st_size == 0:
                raise ValueError("视频文件为空")
        except Exception:
            record.status = "failed"
            record.error = "视频上传未完成，请重新上传"
            record.stages.upload = "failed"
            self.write(record)
            raise
        record.stages.upload = "completed"
        self.write(record)
        self.start(record.task_id)
        return self.public(record)

    @staticmethod
    def _copy_upload(path, stream):
        with path.open("wb") as target:
            shutil.copyfileobj(stream, target)

    async def adopt(self, request):
        source = IndexedChapterSource(await asyncio.to_thread(self.adapter_provider))
        status = await source.aget_asset_status(
            workspace=request.library_id, asset_id=request.asset_id
        )
        if status is None:
            raise KeyError("找不到该索引视频")
        state = getattr(status.status, "value", status.status)
        if (
            state != "completed"
            or not status.sop_path
            or not status.file_path
            or not Path(status.sop_path).is_file()
            or not Path(status.file_path).is_file()
        ):
            raise ValueError("视频索引尚未完成或源文件不可用")
        course_id = fingerprint([request.library_id, request.asset_id])
        with file_lock(self.settings()[0] / (course_id + ".submission.lock")):
            for task in self.list():
                if (
                    task.course_id == course_id
                    and task.background != request.background
                    and task.status in {"queued", "processing"}
                ):
                    raise ValueError("该课程正在处理中，请完成后再修改背景")
                if task.course_id == course_id and task.background == request.background:
                    if task.status in {"queued", "processing"}:
                        self.start(task.task_id)
                    if task.status == "ready":
                        record = self.read(task.task_id)
                        source_hash = fingerprint(json.loads(Path(status.sop_path).read_text()))
                        if record.source_sha256 != source_hash:
                            continue
                        if not self.artifacts_ready(record):
                            record.status, record.error = "queued", None
                            self.write(record)
                            self.start(record.task_id)
                            return self.public(record)
                    return task
            record = self._new(
                request.library_id,
                request.asset_id,
                str(status.file_title or request.asset_id),
                request.background,
                video_path=str(status.file_path),
                sop_path=str(status.sop_path),
                stages=CourseStages(
                    upload="not_applicable", normalization="not_applicable", indexing="completed"
                ),
            )
            self.start(record.task_id)
            return self.public(record)

    def _chapter_result(self, record):
        if not record.chapter_job_key:
            return self.chapters.read(record.library_id, record.asset_id)
        root, config = self.chapters.settings()
        result = ChapterJobs(root, config, None).published_view(record.library_id, record.asset_id)
        if (
            result["status"] != "completed"
            or result["job_key"] != record.chapter_job_key
            or result["source_sha256"] != record.source_sha256
        ):
            raise ValueError("待补充讲稿的原章节版本已变更")
        return result

    def artifacts_ready(self, record):
        try:
            if not record.sop_path:
                return False
            source = json.loads(Path(record.sop_path).read_text())
            if fingerprint(source) != record.source_sha256:
                return False
            rows, version = transcript_input(source, record.asset_id)
            plan = json.loads(Path(record.sop_path).with_name("reading-plan.json").read_text())
            validate_plan(plan, record.asset_id, version, sentences(rows), require_review=True)
            if plan.get("background_sha256") != fingerprint(record.background.model_dump()):
                return False
            if not rows and plan.get("speech_status") != "no_audio":
                return False
            result = self._chapter_result(record)
            if result["status"] != "completed":
                return False
            if result.get("job_key"):
                root, config = self.chapters.settings()
                jobs = ChapterJobs(root, config, None)
                published = jobs.published_view(record.library_id, record.asset_id)
                if (
                    published["status"] != "completed"
                    or published["job_key"] != result["job_key"]
                    or published["source_sha256"] != record.source_sha256
                ):
                    return False
                immutable = json.loads(
                    (
                        jobs.asset_dir(record.library_id, record.asset_id)
                        / published["job_key"]
                        / "reading-plan.json"
                    ).read_text()
                )
                validate_plan(
                    immutable, record.asset_id, version, sentences(rows), require_review=True
                )
                if immutable.get("background_sha256") != fingerprint(
                    record.background.model_dump()
                ):
                    return False
                run_dir, binding, skip_topics = self._topic_source(record, result)
                if (
                    not skip_topics
                    and read_topics(
                        run_dir / "semantic-outline.json", result["chapters"], **binding
                    )
                    is None
                ):
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    async def prepare(self, reader, course_id, background=None):
        # The catalog validates manifest, published source checksum, and video path.
        # Public requests carry only course identity and optional background.
        source = await asyncio.to_thread(reader.preparation_source, course_id)
        library_id, asset_id = source["workspace"], source["asset_id"]
        identity = fingerprint([library_id, asset_id])
        background = background or CourseBackground.model_validate(source.get("background", {}))
        source_hash = fingerprint(source["payload"])
        with file_lock(self.settings()[0] / (identity + ".submission.lock")):
            for task in self.list():
                if task.course_id != identity:
                    continue
                existing = self.read(task.task_id)
                if task.status in {"queued", "processing"}:
                    if task.background != background:
                        raise ValueError("该课程正在处理中，请完成后再修改背景")
                    self.start(task.task_id)
                    return task
                if task.background == background and existing.source_sha256 == source_hash:
                    if self.artifacts_ready(existing):
                        return self.public(existing)
                    if existing.snapshot is not None:
                        try:
                            existing.snapshot.read()
                        except (OSError, ValueError):
                            pass
                        else:
                            existing.status, existing.error = "queued", None
                            self.write(existing)
                            self.start(existing.task_id)
                            return self.public(existing)
            record = self._new(
                library_id,
                asset_id,
                source["title"],
                background,
                stages=CourseStages(
                    upload="not_applicable", normalization="not_applicable", indexing="completed"
                ),
            )
            # Copy the validated published source once, so subsequent reindexing or
            # immutable sample resources cannot mutate the accepted preparation job.
            pinned_path = self.path(record.task_id).parent / "source" / "sop-full.json"
            save(pinned_path, source["payload"])
            record.snapshot = ChapterSnapshot(
                sop_path=pinned_path,
                video_path=Path(source["video_path"]),
                file_title=source["title"],
                duration_seconds=source["duration"],
                source_sha256=source_hash,
            )
            record.source_sha256 = source_hash
            record.sop_path, record.video_path = str(pinned_path), str(source["video_path"])
            if source.get("chapter_job_key") and background == CourseBackground.model_validate(
                source.get("background", {})
            ):
                record.chapter_job_key = source["chapter_job_key"]
                self._chapter_result(record)  # Revalidate publication before accepting reuse.
                record.stages.titles = record.stages.chapters = "completed"
                plan = source.get("reading_plan")
                if plan and plan.get("background_sha256") == fingerprint(background.model_dump()):
                    save(pinned_path.with_name("reading-plan.json"), plan)
            self.write(record)
            self.start(record.task_id)
            return self.public(record)

    @staticmethod
    async def _worker_call(method, *args, **kwargs):
        """Keep sync stores/cold adapter setup off the API loop; drain before releasing leases."""

        def run():
            return asyncio.run(method(*args, **kwargs))

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def start(self, task_id):
        if self.closing:
            raise RuntimeError("course service is closing")
        if task_id not in self.running:
            task = asyncio.create_task(self._run(task_id))
            self.running[task_id] = task
            task.add_done_callback(lambda done: self.running.pop(task_id, None))

    async def retry(self, task_id):
        record = self.read(task_id)
        if record.status == "ready" and record.translation_quality == "needs_review":
            record.translation_attempt += 1
            record.stages.translation = "pending"
            record.status, record.error = "queued", None
            self.write(record)
        if record.status == "failed":
            if record.error_code == "asr_missing" and record.sop_path:
                current = fingerprint(json.loads(Path(record.sop_path).read_text()))
                if current == record.source_sha256:
                    raise ValueError(
                        "原始 ASR 仍为空，请先检查 ASR 并重建该视频索引，再重试课程处理"
                    )
            record.error_code, record.retryable = None, True
            if record.stages.upload == "failed":
                raise ValueError("上传不完整，请重新上传文件")
            if record.stages.translation == "failed":
                record.translation_attempt += 1
            record.status, record.error = "queued", None
            self.write(record)
        if record.status in {"queued", "processing"}:
            self.start(task_id)
        return self.public(record)

    async def recover(self):
        if self.config_provider().capabilities.vicoach.chapters is None:
            return
        for task in self.list():
            record = self.read(task.task_id)
            if task.status == "ready":
                if not await asyncio.to_thread(self.artifacts_ready, record):
                    # Complete old derivatives in the worker, never during GET.
                    record.status, record.error = "queued", None
                    self.write(record)
                    self.start(task.task_id)
                elif record.stages.topics == "pending":
                    result = await asyncio.to_thread(self._chapter_result, record)
                    skipped = not result.get("job_key") or self._topic_source(record, result)[2]
                    record.stages.topics = "not_applicable" if skipped else "completed"
                    self.write(record)
            elif task.status in {"queued", "processing"}:
                if record.upload_id and record.stages.upload != "completed":
                    record.status, record.error = "failed", "上传被中断，请重新上传"
                    record.stages.upload = "failed"
                    self.write(record)
                else:
                    self.start(task.task_id)

    async def close(self):
        self.closing = True
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, task_id):
        # Keep an OS lease for the lifetime of this coroutine: multiple API workers
        # can recover the same persisted task without duplicating model/index jobs.
        initial = self.read(task_id)
        lock_path = self.settings()[0] / (initial.course_id + ".worker.lock")
        with lock_path.open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                record = self.read(task_id)
                if task_id not in {
                    t.task_id
                    for t in self.list()
                    if t.course_id == initial.course_id and t.status == "processing"
                }:
                    record.status, record.error = "failed", "该课程已有后台任务正在处理，请稍后重试"
                    self.write(record)
                return
            record = self.read(task_id)
            if record.status == "ready":
                return
            record.status, record.error = "processing", None
            self.write(record)
            try:
                if record.stages.indexing != "completed":
                    await self._index(record)
                await self._derive(record)
                record.status = "ready"
            except asyncio.CancelledError:
                # Queued work survives shutdown; do not label an interruption failure.
                record.status = "queued"
                raise
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)[:400]
                for key, value in record.stages.model_dump().items():
                    if value == "running":
                        setattr(record.stages, key, "failed")
            finally:
                self.write(record)

    async def _index(self, record):
        manager = await asyncio.to_thread(self.upload_provider)
        params = {
            "course_background": record.background.model_dump(),
            "segmentation_mode": record.segmentation_mode,
        }
        upload = await asyncio.to_thread(manager.get_upload, record.upload_id)
        record.stages.normalization = "running"
        self.write(record)
        if upload is None:
            with Path(record.source_path).open("rb") as stream:
                upload = await self._worker_call(
                    manager.upload_stream,
                    filename=record.title,
                    stream=stream,
                    library_id=record.library_id,
                    upload_id=record.upload_id,
                    index_params=params,
                )
        elif str(upload.status) == "failed":
            upload = await self._worker_call(
                manager.retry_index, record.upload_id, index_params=params
            )
        elif str(upload.status) in {"uploading", "normalizing"}:
            upload = await self._worker_call(
                manager.complete_upload, record.upload_id, index_params=params
            )
        elif str(upload.status) == "indexing":
            # A persisted submission survives a process restart, but lazy workers
            # do not. Resume consumption without submitting the asset a second time.
            adapter = await asyncio.to_thread(self.adapter_provider)
            await adapter.start_background_runtime()
        while True:
            upload = await asyncio.to_thread(manager.get_upload, record.upload_id)
            if upload is None:
                raise ValueError("上传记录不存在")
            state = str(upload.status)
            if state == "failed":
                raise ValueError(upload.error_message or "建立索引失败")
            record.video_path = upload.local_path
            if state in {"indexing", "done"}:
                record.stages.normalization = "completed"
                record.stages.indexing = "running"
                self.write(record)
            if state == "done":
                record.stages.indexing = "completed"
                self.write(record)
                return
            await asyncio.sleep(self.settings()[1].poll_seconds)

    async def _derive(self, record):
        if record.snapshot is not None:
            status, sop = await asyncio.to_thread(record.snapshot.read)
        else:
            source = IndexedChapterSource(await asyncio.to_thread(self.adapter_provider))
            status = await source.aget_asset_status(
                workspace=record.library_id, asset_id=record.asset_id
            )
            if status is None or not status.sop_path:
                raise ValueError("已建立索引，但找不到原始解析结果")
            sop = await asyncio.to_thread(lambda: json.loads(Path(status.sop_path).read_text()))
        record.video_path, record.sop_path = str(status.file_path), str(status.sop_path)
        sop_path = Path(record.sop_path)
        if not sop_path.is_file() or not Path(record.video_path).is_file():
            raise ValueError("课程源视频或解析结果不可用")
        source_hash = fingerprint(sop)
        if record.source_sha256 and record.source_sha256 != source_hash:
            record.stages.translation = "pending"
            record.stages.chapters = "pending"
            record.stages.titles = "pending"
            record.stages.topics = "pending"
        record.source_sha256 = source_hash
        save(sop_path.with_name("course-background.json"), record.background.model_dump())
        self.write(record)
        # Derivative jobs share immutable source ASR but retry independently.
        results = await asyncio.gather(
            self._translation(record, sop), self._chapters(record), return_exceptions=True
        )
        failures = [str(r) for r in results if isinstance(r, Exception)]
        if failures:
            raise ValueError("；".join(failures))
        # Publish aligned translations into the same immutable generation directory.
        # Snapshot readers then retain the last good bilingual lesson on later failure.
        result = self._chapter_result(record)
        if result.get("job_key"):
            run_dir, binding, skip_topics = self._topic_source(record, result)
            topic_path = sop_path.with_name("semantic-outline.json")
            record.stages.topics = "not_applicable" if skip_topics else "running"
            self.write(record)
            if not skip_topics:
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        self.topic_generator,
                        result["chapters"],
                        path=topic_path,
                        config=self.settings()[2],
                        **binding,
                    )
                )
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    await worker
                    raise
            with file_lock(run_dir.parent / "publication.lock"):
                pointer = run_dir.parent / "latest.json"
                if record.chapter_job_key and (run_dir.parent / "published.json").exists():
                    pointer = run_dir.parent / "published.json"
                current = json.loads(pointer.read_text())["job_key"]
                if current != result["job_key"]:
                    raise ValueError("课程已被新的处理任务替代")
                _, current_binding, current_skip = self._topic_source(record, result)
                if current_binding != binding or current_skip != skip_topics:
                    raise ValueError("课程章节版本在主题生成期间已改变")
                topics = None
                if not skip_topics:
                    topics = read_topics(topic_path, result["chapters"], **binding)
                    if topics is None:
                        raise ValueError("语义目录校验失败")
                plan = json.loads(sop_path.with_name("reading-plan.json").read_text())
                rows, version = transcript_input(sop, record.asset_id)
                validate_plan(plan, record.asset_id, version, sentences(rows), require_review=True)
                save(run_dir / "reading-plan.json", plan)
                if topics is not None:
                    save(run_dir / "semantic-outline.json", topics)
                    record.stages.topics = "completed"
                save(run_dir.parent / "published.json", {"job_key": result["job_key"]})

    def _topic_source(self, record, result):
        root, _ = self.chapters.settings()
        run_dir = root / record.course_id / result["job_key"]
        job = json.loads((run_dir / "job.json").read_text())
        if (
            job["job_key"] != result["job_key"]
            or job["source_sha256"] != record.source_sha256
            or job["asset_id"] != record.asset_id
            or job["workspace"] != record.library_id
            or job["status"] != "completed"
            or fingerprint(json.loads((run_dir / "source.json").read_text()))
            != record.source_sha256
        ):
            raise ValueError("语义目录的原章节来源已改变")
        chapter_path = run_dir / "charpter.json"
        if not chapter_path.exists():
            chapter_path = Path(job["sop_path"]).with_name("charpter.json")
        raw = chapter_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != job["export_sha256"]:
            raise ValueError("语义目录的原章节版本已改变")
        leaves = TypeAdapter(list[LearningChapter]).validate_json(raw)
        if [c.model_dump(mode="json") for c in leaves] != result["chapters"]:
            raise ValueError("语义目录的原章节内容已改变")
        outline_path = run_dir / "slide-outline.json"
        outline = json.loads(outline_path.read_text()) if outline_path.exists() else {}
        if job.get("outline_sha256") and (
            not outline_path.is_file()
            or hashlib.sha256(outline_path.read_bytes()).hexdigest() != job["outline_sha256"]
        ):
            raise ValueError("原编号目录校验失败")
        if outline and (
            outline.get("source_sha256") != record.source_sha256
            or outline.get("chapter_version") != job["export_sha256"]
        ):
            raise ValueError("原编号目录与章节版本不一致")
        return (
            run_dir,
            {
                "asset_id": record.asset_id,
                "source_sha256": record.source_sha256,
                "chapter_version": job["export_sha256"],
                "background": record.background.model_dump(),
            },
            not needs_topic_grouping(result["chapters"], outline),
        )

    async def _translation(self, record, sop):
        record.stages.translation = "running"
        self.write(record)
        _, options, model = self.settings()
        try:
            # Shield the thread so shutdown cannot release its lease while a model
            # call still publishes. It is drained before returning cancellation.
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self.translator,
                    sop,
                    record.asset_id,
                    Path(record.video_path),
                    Path(record.sop_path).with_name("reading-plan.json"),
                    record.background,
                    model,
                    options.translation_batch_size,
                    translation_attempt=record.translation_attempt,
                )
            )
            try:
                plan = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            record.speech_status = plan["speech_status"]
            record.translation_quality = plan.get("translation_quality", "model_reviewed")
            record.translation_review_count = len(
                {
                    issue["sentence_id"]
                    for issue in (plan.get("semantic_review") or {}).get("translation_issues", [])
                }
            )
            record.stages.translation = "completed"
        except MissingTranscript:
            record.error_code, record.retryable = "asr_missing", False
            record.stages.translation = "failed"
            raise
        except Exception:
            record.stages.translation = "failed"
            raise
        finally:
            self.write(record)

    async def _chapters(self, record):
        if record.stages.chapters == "completed":
            existing = await asyncio.to_thread(self._chapter_result, record)
            if existing["status"] == "completed":
                return
        record.stages.titles = "running"
        record.stages.chapters = "pending"
        self.write(record)
        try:
            if record.snapshot is not None:
                result = await self.chapters.generate_snapshot(
                    record.library_id,
                    record.asset_id,
                    record.snapshot.model_dump(mode="json"),
                    publish_course=False,
                )
            else:
                result = await self.chapters.generate(
                    record.library_id, record.asset_id, publish_course=False
                )
            while result["status"] in {"queued", "running"}:
                phase = result.get("phase")
                if phase in {"chapters", "publishing", "completed"}:
                    record.stages.titles = "completed"
                    record.stages.chapters = "running"
                self.write(record)
                await asyncio.sleep(self.settings()[1].poll_seconds)
                result = await asyncio.to_thread(
                    self.chapters.read, record.library_id, record.asset_id
                )
            if result["status"] != "completed":
                raise ValueError(result.get("error") or "章节生成失败")
            record.stages.titles = "completed"
            record.stages.chapters = "completed"
        except Exception:
            if record.stages.titles != "completed":
                record.stages.titles = "failed"
            record.stages.chapters = "failed"
            raise
        finally:
            self.write(record)

    def video(self, task_id):
        record = self.read(task_id)
        path = Path(record.video_path or record.source_path or "")
        if not path.is_file():
            raise KeyError("视频尚不可预览")
        return path
