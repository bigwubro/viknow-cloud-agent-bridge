"""HTTP boundary for the course workbench, mounted in the standard ViKnow app."""

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from viknow.capabilities.vicoach.chapters.jobs import ChapterJobs
from viknow.capabilities.vicoach.courses import AdoptCourse, CourseTask, PrepareCourse
from viknow.capabilities.vicoach.grading import (
    JudgeRequest,
    JudgeResult,
    JudgeUnavailable,
    find_open_question,
    judge_answer,
)
from viknow.capabilities.vicoach.snapshot.catalog import CourseCatalog, CourseSummary
from viknow.capabilities.vicoach.snapshot.models import Lesson
from viknow.capabilities.vicoach.snapshot.service import load_lesson
from viknow.capabilities.vicoach.translations import CourseBackground


def create_coach_router(web_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/coach", tags=["coach"])
    workbench = web_dir / "course-workbench"
    bundle = workbench / "sample"

    @router.get("", include_in_schema=False)
    def redirect(request: Request):
        return RedirectResponse(request.url_for("coach_page"))

    def file(path: Path, media_type: str):
        if not path.is_file():
            raise HTTPException(503, "课程样例资源尚未安装")
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @router.api_route("/", methods=["GET", "HEAD"], name="coach_page", include_in_schema=False)
    def page():
        return file(workbench / "index.html", "text/html")

    @router.get("/lesson.json", response_model=Lesson)
    def lesson():
        try:
            result = load_lesson(bundle)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, "课程样例数据缺失或来源校验失败") from exc
        return result

    def catalog(request: Request):
        service = getattr(request.app.state, "learning_chapters", None)
        jobs = None
        if service is not None:
            try:
                root, config = service.settings()
            except RuntimeError:
                pass  # The bundled example remains available when learning is disabled.
            else:
                jobs = ChapterJobs(root, config, None)
        return CourseCatalog(bundle, jobs, getattr(request.app.state, "coach_courses", None))

    def course_service(request):
        service = getattr(request.app.state, "coach_courses", None)
        if service is None:
            raise HTTPException(503, "课程后台处理尚未配置")
        try:
            service.settings()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return service

    @router.post("/uploads", response_model=CourseTask, status_code=202)
    async def upload_course(
        request: Request,
        file: Annotated[UploadFile, File()],
        introduction: str = Form(""),
        learning_goals: str = Form(""),
        audience: str = Form(""),
        library_id: str | None = Form(None),
        segmentation_mode: Literal["general", "presentation"] = Form("general"),
    ):
        try:
            background = CourseBackground(
                introduction=introduction, learning_goals=learning_goals, audience=audience
            )
            return await course_service(request).upload(
                file.filename, file.file, background, library_id, segmentation_mode
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.post("/courses/adopt", response_model=CourseTask, status_code=202)
    async def adopt_course(request: Request, payload: AdoptCourse):
        try:
            return await course_service(request).adopt(payload)
        except KeyError as exc:
            raise HTTPException(404, "找不到该索引视频") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/courses/{course_id}/prepare", response_model=CourseTask, status_code=202)
    async def prepare_course(
        request: Request, course_id: str, payload: Annotated[PrepareCourse | None, Body()] = None
    ):
        try:
            return await course_service(request).prepare(
                catalog(request), course_id, payload.background if payload else None
            )
        except (OSError, KeyError) as exc:
            raise HTTPException(404, "课程来源不可用") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/tasks", response_model=list[CourseTask])
    def tasks(request: Request):
        return course_service(request).list()

    @router.get("/tasks/{task_id}", response_model=CourseTask)
    def task(request: Request, task_id: str):
        try:
            service = course_service(request)
            return service.public(service.read(task_id))
        except (OSError, ValueError) as exc:
            raise HTTPException(404, "课程任务不存在") from exc

    @router.post("/tasks/{task_id}/retry", response_model=CourseTask)
    async def retry_task(request: Request, task_id: str):
        try:
            return await course_service(request).retry(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "课程任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.api_route("/tasks/{task_id}/video.mp4", methods=["GET", "HEAD"])
    def preview(request: Request, task_id: str):
        try:
            return file(course_service(request).video(task_id), "video/mp4")
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(404, "视频暂不可预览") from exc

    @router.get("/courses.json", response_model=list[CourseSummary])
    def courses(request: Request):
        return catalog(request).courses()

    @router.get("/courses/{course_id}/lesson.json", response_model=Lesson)
    def course_lesson(request: Request, course_id: str):
        try:
            return catalog(request).lesson(course_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(404, "课程不存在或章节已失效，请刷新视频列表") from exc

    @router.post("/courses/{course_id}/judge", response_model=JudgeResult)
    def score_open_answer(request: Request, course_id: str, payload: JudgeRequest):
        reader = catalog(request)
        try:
            lesson = reader.lesson(course_id)
            report = json.loads(reader.resource(course_id, "questions.json").read_text())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(404, "该课程或练习题不可用") from exc
        if (payload.asset_id, payload.chapter_version) != (lesson.asset_id, lesson.chapter_version):
            raise HTTPException(409, "课程版本已变化，请重新加载后作答")
        try:
            question = find_open_question(report, payload)
        except KeyError as exc:
            raise HTTPException(404, "题目不存在") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, "题目版本、题型或参考答案不可用于评分") from exc
        service = getattr(request.app.state, "learning_chapters", None)
        if service is None:
            raise HTTPException(503, "简答题评分模型尚未配置")
        try:
            _, config = service.settings()
        except RuntimeError as exc:
            raise HTTPException(503, "简答题评分模型尚未配置") from exc
        try:
            return judge_answer(question, payload.submitted_answer, config)
        except JudgeUnavailable as exc:
            raise HTTPException(503, "AI 评分暂时不可用，请稍后重新提交；当前答案已保留") from exc

    @router.api_route("/courses/{course_id}/{name}", methods=["GET", "HEAD"])
    def course_resource(request: Request, course_id: str, name: str):
        try:
            path = catalog(request).resource(course_id, name)
            if not path.is_file():
                raise KeyError("resource not found")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(404, "该课程资源尚未生成或已失效") from exc
        return file(path, "video/mp4" if name == "video.mp4" else "application/json")

    # Never mount the entire sample directory: SOP and provenance manifests
    # are internal inputs, not public static resources.
    assets = {
        "style.css": (workbench / "style.css", "text/css"),
        "app.mjs": (workbench / "app.mjs", "text/javascript"),
        "training.mjs": (workbench / "training.mjs", "text/javascript"),
        "courses.mjs": (workbench / "courses.mjs", "text/javascript"),
        "model.mjs": (workbench / "model.mjs", "text/javascript"),
        "player.mjs": (workbench / "player.mjs", "text/javascript"),
        "transcript.mjs": (workbench / "transcript.mjs", "text/javascript"),
        "questions.mjs": (workbench / "questions.mjs", "text/javascript"),
        "questions.json": (workbench / "questions.sample.json", "application/json"),
        "reading-plan.json": (workbench / "public-health15.reading-plan.json", "application/json"),
        "video.mp4": (bundle / "video.mp4", "video/mp4"),
        "poster.jpg": (bundle / "poster.jpg", "image/jpeg"),
    }

    @router.api_route("/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def asset(name: str):
        selected = assets.get(name)
        if selected is None:
            raise HTTPException(404, "资源不存在")
        return file(*selected)

    return router
