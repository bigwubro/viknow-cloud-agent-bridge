"""Admin API for the first human candidate review gate."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from viknow.api.admin import require_admin_auth
from viknow.core.learning.case_review import (
    ApprovedCaseSnapshot,
    CandidateCase,
    CaseReviewDecision,
    OptimizationAnalysisTask,
    OptimizationProposal,
    OracleRequest,
    ReviewStatus,
    SkillOptimizationExperiment,
    SkillOptimizationExperimentConfig,
    SkillOptimizationTask,
)
from viknow.core.learning.case_review_service import CaseReviewService
from viknow.core.learning.skill_optimization_view import SkillOptimizationExperimentView


class CaseDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ReviewStatus
    reason: str | None = Field(default=None, max_length=2000)


class RemoveCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


class CandidateLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(max_length=100)


class CandidateAnalysisBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=1, max_length=100)


class SkillOptimizationExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=1, max_length=100)
    config: SkillOptimizationExperimentConfig


class OracleGateBDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "revised", "rejected"]
    accepted_fact_ids: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)


class SkillOptimizationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    note: str | None = Field(default=None, max_length=2000)


class SkillOptimizationExperimentDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["keep", "reject"]
    note: str | None = Field(default=None, max_length=2000)


class ReferenceAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str | None = Field(default=None, max_length=20_000)


class CandidateDetail(BaseModel):
    candidate: CandidateCase
    decisions: list[CaseReviewDecision]
    snapshot: ApprovedCaseSnapshot | None = None
    proposal: OptimizationProposal | None = None
    analysis_task: OptimizationAnalysisTask | None = None
    oracle_request: OracleRequest | None = None
    skill_optimization_task: SkillOptimizationTask | None = None


class RunReviewDetail(BaseModel):
    candidate: CandidateCase | None = None
    decisions: list[CaseReviewDecision] = Field(default_factory=list)
    snapshot: ApprovedCaseSnapshot | None = None
    proposal: OptimizationProposal | None = None
    analysis_task: OptimizationAnalysisTask | None = None
    oracle_request: OracleRequest | None = None
    skill_optimization_task: SkillOptimizationTask | None = None


def _service(request: Request) -> CaseReviewService:
    service = getattr(request.app.state, "case_review_service", None)
    if service is None:
        raise HTTPException(503, "候选审核服务暂不可用")
    return service


def create_case_review_router() -> APIRouter:
    root = APIRouter()
    router = APIRouter(
        prefix="/api/v1/self-learning/candidates",
        tags=["self-learning"],
        dependencies=[Depends(require_admin_auth)],
    )

    @router.get("", response_model=list[CandidateCase])
    def list_candidates(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ):
        return _service(request).list_candidates(limit=limit)

    @router.post("/lookup", response_model=dict[str, CandidateCase])
    def lookup_candidates(payload: CandidateLookupRequest, request: Request):
        return _service(request).candidate_lookup(payload.run_ids)

    @router.post("/{run_id}", response_model=CandidateCase, status_code=status.HTTP_201_CREATED)
    async def add_candidate(run_id: str, request: Request):
        try:
            service = _service(request)
            return service.add_manual_candidate(
                run_id, actor=request.app.state.admin_config.username
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该 Run") from exc
        except ValueError as exc:
            raise HTTPException(409, "只有已结束的 Run 才能加入候选池") from exc

    @router.post(
        "/analysis/batch",
        response_model=list[OptimizationAnalysisTask],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_batch_analysis(
        payload: CandidateAnalysisBatchRequest,
        request: Request,
    ):
        service = _service(request)
        run_ids = list(dict.fromkeys(payload.run_ids))
        try:
            for run_id in run_ids:
                service.candidate(run_id)
            tasks = [service.enqueue_proposal_analysis(run_id) for run_id in run_ids]
        except KeyError as exc:
            raise HTTPException(404, "批次中包含不存在或已取消的候选 Case") from exc
        return tasks

    @router.post(
        "/{run_id}/analysis",
        response_model=OptimizationAnalysisTask,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_analysis(run_id: str, request: Request):
        service = _service(request)
        try:
            task = service.enqueue_proposal_analysis(run_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该候选 Case") from exc
        return task

    @router.delete("/{run_id}/analysis/{task_id}", response_model=OptimizationAnalysisTask)
    def cancel_analysis(run_id: str, task_id: str, request: Request):
        try:
            return _service(request).cancel_proposal_analysis(run_id, task_id=task_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该分析任务") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete("/{run_id}", response_model=CandidateCase)
    def remove_candidate(run_id: str, payload: RemoveCandidateRequest, request: Request):
        try:
            return _service(request).remove_candidate(
                run_id,
                reviewer=request.app.state.admin_config.username,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该候选 Case") from exc
        except ValueError as exc:
            raise HTTPException(409, "只有待审核 Case 可以取消") from exc

    @router.get("/{run_id}", response_model=CandidateDetail)
    def detail(run_id: str, request: Request):
        service = _service(request)
        try:
            candidate = service.candidate(run_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该候选 Case") from exc
        try:
            snapshot = service.snapshot(run_id)
        except KeyError:
            snapshot = None
        try:
            proposal = service.proposal(run_id)
        except KeyError:
            proposal = None
        return CandidateDetail(
            candidate=candidate,
            decisions=service.decisions(run_id),
            snapshot=snapshot,
            proposal=proposal,
            analysis_task=service.latest_analysis_task(run_id),
            oracle_request=service.oracle_request(run_id),
            skill_optimization_task=service.skill_optimization_task(run_id),
        )

    @router.post("/{run_id}/decision", response_model=CandidateCase)
    def decide(run_id: str, payload: CaseDecisionRequest, request: Request):
        try:
            return _service(request).decide(
                run_id,
                status=payload.status,
                reviewer=request.app.state.admin_config.username,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该候选 Case") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/{run_id}/snapshot", response_model=ApprovedCaseSnapshot)
    def snapshot(run_id: str, request: Request):
        try:
            return _service(request).snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(404, "该候选 Case 尚无批准快照") from exc

    @router.post("/{run_id}/oracle/decision", response_model=OracleRequest)
    def decide_oracle(run_id: str, payload: OracleGateBDecisionRequest, request: Request):
        try:
            return _service(request).review_oracle_evidence(
                run_id,
                status=payload.status,
                accepted_fact_ids=payload.accepted_fact_ids,
                reviewer=request.app.state.admin_config.username,
                note=payload.note,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该 Oracle 请求") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post(
        "/{run_id}/optimization",
        status_code=status.HTTP_410_GONE,
    )
    def start_skill_optimization(run_id: str):
        del run_id
        raise HTTPException(410, "单条优化入口已停用，请从素材准备页统一创建优化实验")

    @router.post("/{run_id}/optimization/decision", response_model=SkillOptimizationTask)
    def decide_skill_optimization(
        run_id: str, payload: SkillOptimizationDecisionRequest, request: Request
    ):
        try:
            return _service(request).review_skill_optimization(
                run_id,
                decision=payload.decision,
                reviewer=request.app.state.admin_config.username,
                note=payload.note,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该优化任务") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    reviews = APIRouter(
        prefix="/api/v1/self-learning/reviews",
        tags=["self-learning"],
        dependencies=[Depends(require_admin_auth)],
    )

    @reviews.get("/{run_id}", response_model=RunReviewDetail)
    def review_detail(run_id: str, request: Request):
        service = _service(request)
        try:
            service.require_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该 Run") from exc
        candidate = service.candidate_lookup([run_id]).get(run_id)
        try:
            proposal = service.proposal(run_id)
        except KeyError:
            proposal = None
        try:
            snapshot = service.snapshot(run_id)
        except KeyError:
            snapshot = None
        return RunReviewDetail(
            candidate=candidate,
            decisions=service.decisions(run_id) if candidate is not None else [],
            snapshot=snapshot,
            proposal=proposal,
            analysis_task=service.latest_analysis_task(run_id),
            oracle_request=service.oracle_request(run_id),
            skill_optimization_task=service.skill_optimization_task(run_id),
        )

    @reviews.post(
        "/{run_id}/analysis",
        response_model=OptimizationAnalysisTask,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def analyze_run(run_id: str, request: Request):
        try:
            return _service(request).enqueue_proposal_analysis(run_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该 Run") from exc
        except ValueError as exc:
            raise HTTPException(409, "只有已结束的 Run 才能分析") from exc

    @reviews.post("/{run_id}/gate-a", response_model=CandidateCase)
    def decide_run_gate_a(run_id: str, payload: CaseDecisionRequest, request: Request):
        try:
            return _service(request).decide_gate_a(
                run_id,
                status=payload.status,
                reviewer=request.app.state.admin_config.username,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该 Run 或分析结果") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @reviews.post(
        "/{run_id}/materials/research",
        response_model=OracleRequest,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_material_research(run_id: str, request: Request):
        try:
            return _service(request).start_material_research(run_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到已纳入的候选 Case") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @reviews.put("/{run_id}/materials/reference-answer", response_model=CandidateCase)
    def save_reference_answer(run_id: str, payload: ReferenceAnswerRequest, request: Request):
        try:
            return _service(request).save_reference_answer(run_id, answer=payload.answer)
        except KeyError as exc:
            raise HTTPException(404, "未找到已纳入的候选 Case") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    experiments = APIRouter(
        prefix="/api/v1/self-learning/skill-optimization",
        tags=["self-learning"],
        dependencies=[Depends(require_admin_auth)],
    )

    @experiments.get("/options")
    def experiment_options(request: Request):
        from viknow.core.config import skill_optimization_model_options

        models = skill_optimization_model_options(request.app.state.config)
        defaults = None
        if models:
            defaults = SkillOptimizationExperimentConfig(
                optimizer_model=models[0]["id"],
                target_model=models[0]["id"],
                evaluator_model=models[0]["id"],
            ).model_dump(mode="json")
        return {
            "models": models,
            "defaults": defaults,
            "parameter_schema": SkillOptimizationExperimentConfig.model_json_schema(),
        }

    @experiments.post("/experiments", response_model=SkillOptimizationExperiment, status_code=202)
    def create_experiment(payload: SkillOptimizationExperimentRequest, request: Request):
        from viknow.core.config import skill_optimization_model_options

        registered = {
            item["id"] for item in skill_optimization_model_options(request.app.state.config)
        }
        if any(
            model not in registered
            for model in (
                payload.config.optimizer_model,
                payload.config.target_model,
                payload.config.evaluator_model,
            )
        ):
            raise HTTPException(422, "未知模型；请选择服务端模型目录中的选项")
        try:
            return _service(request).create_skill_optimization_experiment(
                run_ids=payload.run_ids,
                config=payload.config,
                actor=request.app.state.admin_config.username,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到已批准的 Case") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @experiments.get("/experiments", response_model=list[SkillOptimizationExperiment])
    def list_experiments(request: Request, limit: Annotated[int, Query(ge=1, le=500)] = 200):
        return _service(request).list_skill_optimization_experiments(limit=limit)

    @experiments.get("/experiments/{experiment_id}", response_model=SkillOptimizationExperiment)
    def experiment_detail(experiment_id: str, request: Request):
        try:
            return _service(request).skill_optimization_experiment(experiment_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该实验") from exc

    @experiments.get(
        "/experiments/{experiment_id}/view", response_model=SkillOptimizationExperimentView
    )
    def experiment_view(experiment_id: str, request: Request):
        try:
            return _service(request).skill_optimization_experiment_view(experiment_id)
        except KeyError as exc:
            raise HTTPException(404, "未找到该实验") from exc

    @experiments.post(
        "/experiments/{experiment_id}/decision", response_model=SkillOptimizationExperiment
    )
    def experiment_decision(
        experiment_id: str, payload: SkillOptimizationExperimentDecisionRequest, request: Request
    ):
        try:
            return _service(request).review_skill_optimization_experiment(
                experiment_id,
                decision=payload.decision,
                reviewer=request.app.state.admin_config.username,
                note=payload.note,
            )
        except KeyError as exc:
            raise HTTPException(404, "未找到该实验") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    root.include_router(experiments)
    root.include_router(router)
    root.include_router(reviews)
    return root
