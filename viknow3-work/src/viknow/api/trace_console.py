"""Admin-only, read-only self-learning trace browser."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import BaseModel

from viknow.api.admin import require_admin_auth
from viknow.core.learning.case_review import (
    SkillOptimizationAcceptanceReview,
    SkillOptimizationTask,
)
from viknow.core.learning.skill_optimization_view import SkillOptimizationView
from viknow.core.runtime.domain import AgentEvent
from viknow.core.trace_console.links import jaeger_links, langfuse_links
from viknow.core.trace_console.models import (
    EventPage,
    TraceDetail,
    TraceLinks,
    TracePage,
    TraceQuery,
)
from viknow.core.trace_console.reader import TraceReader


class ConsoleSettings(BaseModel):
    refresh_seconds: int


class DetailResponse(BaseModel):
    detail: TraceDetail
    langfuse: TraceLinks


class SkillOptimizationListItem(BaseModel):
    id: str
    source_run_id: str
    input_source: str
    target_skill_id: str
    status: str
    attempts: int
    error_code: str | None
    experiment_id: str | None
    baseline_score: float | None
    candidate_score: float | None
    score_delta: float | None
    acceptance_review: SkillOptimizationAcceptanceReview | None
    updated_at: datetime


def _reader(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        yield request.app.state.trace_console_reader
    except KeyError as exc:
        raise HTTPException(404, "未找到该运行或事件") from exc
    except (psycopg.Error, sqlite3.Error, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "数据库读取暂不可用，请稍后重试") from exc


def create_trace_console_router(web_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/self-learning", include_in_schema=False)
    def page():
        return FileResponse(web_dir / "self-learning.html")

    api = APIRouter(
        prefix="/api/v1/self-learning",
        tags=["self-learning"],
        dependencies=[Depends(require_admin_auth)],
    )

    @api.get("/settings", response_model=ConsoleSettings)
    def settings(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return ConsoleSettings(
            refresh_seconds=request.app.state.config.trace_console.refresh_seconds
        )

    @api.get("/traces", response_model=TracePage)
    def traces(
        query: Annotated[TraceQuery, Query()], reader: Annotated[TraceReader, Depends(_reader)]
    ):
        return reader.list_runs(query)

    @api.get("/optimizations", response_model=list[SkillOptimizationListItem])
    def optimizations(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ):
        service = getattr(request.app.state, "case_review_service", None)
        if service is None:
            raise HTTPException(503, "候选审核服务暂不可用")
        return [
            _skill_optimization_list_item(task)
            for task in service.list_skill_optimizations(limit=limit)
        ]

    @api.get("/optimizations/{run_id}", response_model=SkillOptimizationView)
    def optimization_detail(run_id: str, request: Request):
        service = getattr(request.app.state, "case_review_service", None)
        if service is None:
            raise HTTPException(503, "候选审核服务暂不可用")
        try:
            return service.skill_optimization_view(run_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该 SkillOpt 实验") from exc

    @api.get("/traces/{run_id}", response_model=DetailResponse)
    def detail(run_id: str, request: Request, reader: Annotated[TraceReader, Depends(_reader)]):
        result = reader.detail(run_id)
        return DetailResponse(
            detail=result,
            langfuse=langfuse_links(
                result,
                request.app.state.config.trace_console,
                getattr(request.app.state, "langfuse_project_identity", None),
            ),
        )

    @api.get("/traces/{run_id}/events", response_model=EventPage)
    def events(
        run_id: str,
        reader: Annotated[TraceReader, Depends(_reader)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ):
        return reader.events(run_id, after=after, limit=limit)

    @api.get("/traces/{run_id}/events/{sequence}", response_model=AgentEvent)
    def event(run_id: str, sequence: int, reader: Annotated[TraceReader, Depends(_reader)]):
        return reader.event(run_id, sequence)

    @api.get("/traces/{run_id}/links/jaeger", response_model=TraceLinks)
    def jaeger(run_id: str, request: Request, reader: Annotated[TraceReader, Depends(_reader)]):
        return jaeger_links(reader.run(run_id), request.app.state.config.trace_console)

    router.include_router(api)
    return router


def _skill_optimization_list_item(task: SkillOptimizationTask) -> SkillOptimizationListItem:
    manifest = task.acceptance_manifest or {}
    comparison = manifest.get("comparison")
    if isinstance(comparison, dict):
        baseline = comparison.get("baseline_optimization_score")
        candidate = comparison.get("candidate_optimization_score")
        delta = comparison.get("optimization_score_delta")
    else:
        baseline_record = manifest.get("baseline")
        candidate_record = manifest.get("candidate")
        baseline = baseline_record.get("soft") if isinstance(baseline_record, dict) else None
        candidate = candidate_record.get("soft") if isinstance(candidate_record, dict) else None
        delta = manifest.get("score_delta")
    return SkillOptimizationListItem(
        id=task.id,
        source_run_id=task.source_run_id,
        input_source=task.input_source,
        target_skill_id=task.target_skill_id,
        status=task.status,
        attempts=task.attempts,
        error_code=task.error_code,
        experiment_id=task.experiment_id,
        baseline_score=baseline if isinstance(baseline, (int, float)) else None,
        candidate_score=candidate if isinstance(candidate, (int, float)) else None,
        score_delta=delta if isinstance(delta, (int, float)) else None,
        acceptance_review=task.acceptance_review,
        updated_at=task.updated_at,
    )
